# Three ways to fix the authorization race

The race: two swipes on the same card both read a balance of $10, both decide that $10 fits, and both subtract it. The card has paid out $20 against $10. PR #7 showed this with 20 concurrent $10 swipes on a $100 card, which gave 20 approvals.

This note compares three fixes and records which one shipped.

**The choice: way 2, the conditional `UPDATE`.** It's on main from this PR.

## Results

Setup: 50 concurrent $10 swipes, `uvicorn --workers 4`, Postgres 17.12 in Docker, a 4-vCPU Linux VM. The client is `scripts/time_authorizations.py --runs 5`. Each cell is the median of 5 bursts. The whole set was run twice, in opposite orders (lock → serializable, then serializable → lock), and each cell shows `pass 1 / pass 2`. Bench commit: `b795e4f` on `bench/race-strategies`.

"One card" means all 50 swipes hit the same card, which is the worst case for contention. "A card each" means one card per swipe, so nothing conflicts and the numbers show the cost of the request itself.

| | Correct? | Race test, 20 runs | p50, one card (ms) | p99, one card (ms) | p50, a card each (ms) | p99, a card each (ms) | Retries per 50 swipes | 503s per 50 swipes |
|---|---|---|---|---|---|---|---|---|
| 1. `SELECT ... FOR UPDATE` | Yes | 20 / 20 | 612 / 461 | 922 / 733 | 377 / 398 | 495 / 574 | 0 | 0 |
| 2. Conditional `UPDATE` | Yes | 20 / 20 | 438 / 474 | 635 / 670 | 335 / 326 | 520 / 442 | 0 | 0 |
| 3. `SERIALIZABLE` + 3 retries | Money yes, availability no | 3 / 20 | 972 / 937 | 1186 / 1204 | 502 / 443 | 647 / 573 | 106–120 (one card), 0–1 (a card each) | 25–32 (one card), 0 (a card each) |

What the columns mean:
- **Correct?** In every burst of every way, each card's final balance matched its approvals. No way ever over-approved. Way 3 keeps the money right, but it turns away about half the swipes on a busy card. Those swipes had the funds.
- **Race test, 20 runs:** `tests/concurrency/test_authorization_race.py` run with `--count 20`. Way 3 fails because the test expects every response to be a 200, and some are 503s.
- **Retries:** counted from a bench-only `X-Authz-Retries` response header. Ways 1 and 2 wait for the lock instead of retrying, so they never retry.

How to read the numbers:
- The noise is large. Way 1's one-card p50 moved from 612 to 461 ms between passes. The gap between ways 1 and 2 on one card is within that noise. Way 2's p99 was lower in both passes, and it was a little faster with a card each.
- Way 3 is clearly worse under contention. It's about twice as slow and drops about half the swipes.
- Even with no contention, a request takes about 330–400 ms. That was already flagged in PR #9 and still hasn't been looked into. It's the same for all three ways, so it doesn't change the comparison.

## Way 1: lock the row (PR #9)

```python
statement = select(CardRow).where(CardRow.id == request.card_id).with_for_update()
card = session.exec(statement).first()
if card is None:
    decline_reason = "card_not_found"
elif request.amount_cents > card.available_cents:
    decline_reason = "insufficient_funds"
else:
    decline_reason = None
    card.available_cents -= request.amount_cents  # UPDATE sent at commit
```

The `SELECT` takes the row lock. The second swipe on the same card blocks at its own `SELECT` until the first one commits, then reads the new balance.

It's correct, but it takes two round trips to Postgres (`SELECT`, then `UPDATE`), with Python running in between, while the lock is held.

## Way 2: one conditional UPDATE (shipped)

```python
statement = (
    update(CardRow)
    .where(
        col(CardRow.id) == request.card_id,
        col(CardRow.available_cents) >= request.amount_cents,
    )
    .values(available_cents=col(CardRow.available_cents) - request.amount_cents)
    .returning(col(CardRow.available_cents))
)
new_balance_cents = session.exec(statement).scalar_one_or_none()
# None: decline. A follow-up SELECT tells card_not_found from insufficient_funds.
```

The check and the subtraction happen in one statement, inside Postgres. Under `READ COMMITTED`, a second `UPDATE` on a row that's locked waits for the first to commit. Then it **re-evaluates the `WHERE` against the new row version**. So it either subtracts from the real balance or matches nothing. Nothing can see a stale balance, and nothing needs a retry.

The extra `SELECT` runs only on a decline, to choose the reason. That read can't cause harm, because a decline changes no money.

It's one round trip on the approve path, the lock is held for less time, and it follows hard rule 3 word for word. It also can't overflow: an amount near the BIGINT maximum fails the `WHERE` before the subtraction runs (there's a test for this).

## Way 3: SERIALIZABLE and retry (not shipped)

```python
for attempt in range(MAX_RETRIES + 1):  # 1 try + 3 retries
    session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
    try:
        card = session.exec(
            select(CardRow).where(CardRow.id == request.card_id)
        ).first()
        ...  # plain read-check-write, no lock
        return _record(session, request, decline_reason), attempt
    except OperationalError as e:
        session.rollback()
        if getattr(e.orig, "sqlstate", None) != "40001":
            raise
        if attempt < MAX_RETRIES:
            time.sleep(random.uniform(0, 0.005 * 2**attempt))  # jittered backoff
raise AuthorizationContendedError  # -> 503, nothing written
```

Postgres runs the transactions as though they happened one at a time. When two of them read the same balance and both write it, one gets SQLSTATE `40001` (serialization failure) and has to start over. The error can come at the `UPDATE` or at the commit, so the whole attempt sits inside the `try`.

Why it drops swipes when many hit one card: each round, only one of the swipes in flight on that card can commit, and every other one fails and retries. With 50 swipes and 4 attempts each, only about 20–25 ever get through. The rest return 503 with nothing written, which was our decision for this case, so the network can retry or stand in.

More retries or a longer backoff would cut the 503s, but would make the latency worse. The hot-card problem stays either way.

There were also 0–1 retries with a card each, where no two swipes touched the same row. That's probably a false positive. Postgres tracks reads for serializable checking at a coarser level than single rows (whole index pages, for example), so it can flag transactions that touched different rows. It's harmless, but it means any `SERIALIZABLE` code has to handle retries even when there's no real conflict.

Its strength is that the read-check-write code stays as simple as it looks, and it keeps working as the logic grows (holds, limits, several rows per transaction). For a single counter on a hot row, it's the wrong tool.

## Why way 2

- **Correct** with no retries and no 503s.
- **As fast as way 1 or faster.** On one card the difference was within the noise, its p99 was lower in both passes, and it was a little faster with a card each.
- **One statement** carries the invariant, so a refactor can't quietly add a gap between the read and the write.

Its limit: it covers one row and one condition. Once an authorization has to check several rows (a hold table, or usage limits once Redis is in), we'll either chain conditional statements in one transaction or go back to a row lock. That choice gets revisited then.

## Rerunning the numbers

```bash
git switch bench/race-strategies
docker compose up -d
AUTHZ_RACE_STRATEGY=conditional uv run uvicorn authz.main:app --workers 4   # or lock / serializable
uv run python scripts/time_authorizations.py --runs 5
uv run python scripts/time_authorizations.py --runs 5 --card-per-request
AUTHZ_RACE_STRATEGY=serializable uv run pytest tests/concurrency --count 20
```

`bench/race-strategies` is pushed but won't be merged. It has all three functions, the strategy setting, the retry header and the 503 handler.
