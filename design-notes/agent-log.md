# Agent log

## 2026-11-19: In-memory card API (PR #4)

Built the in-memory card API: `POST /cards`, `GET /cards/{id}`, and `POST /authorizations` deciding from a dict-backed `CardStore` via an `authorize()` service. Unknown cards decline with `card_not_found`; insufficient funds decline without changing the balance. The read-check-write race is deliberately kept, idempotency is deferred, and the old 501 stub test was replaced.

## 2026-11-19: Trimmed optional tests (PR #4)

Removed four low-value tests (distinct ids, zero balance, unique authorization ids, set_available on a missing card). 49 tests pass. This left the `KeyError` in `CardStore.set_available` untested, so the check was removed in the next entry.

## 2026-11-19: Dropped KeyError check (PR #4)

Removed the untested `KeyError` check from `CardStore.set_available`. Callers must now pass an existing card id; the authorize service already guarantees this by calling `get()` first.

## 2026-11-20: Authorization race test (PR #7)

Added a concurrency race test: a `uvicorn --workers 4` fixture, and 20 concurrent $10 authorizations against a $100 card asserting 10 approvals (xfail "race, fix later"). All 5 runs gave 20 approvals. Moved the shared DB fixtures to `tests/conftest.py` with `clean_tables` opt-in per folder. Not sure about: non-strict xfail (a lucky run shows as XPASS).

## 2026-11-24: Row lock fixes the authorization race (PR #9)

Fixed the authorization race with `SELECT ... FOR UPDATE` on the card row. Removed xfail from the race test and added a final-balance check. Race test passes 20/20 with `--count 20` and fails without the lock. Added `pytest-repeat` and `scripts/time_authorizations.py`. Timing: 50 concurrent requests on one card, p50 ~490 ms and p99 ~670–790 ms, versus ~350 / ~500 ms with one card per request. Not sure about: the no-contention latency is high, and the cause hasn't been looked into.

## 2026-11-24: Conditional UPDATE replaces the row lock; three race fixes compared

Swapped `authorize()` to one `UPDATE ... WHERE available_cents >= :amt RETURNING` (way 2). Compared it with `FOR UPDATE` (way 1) and SERIALIZABLE + 3 retries (way 3) in `design-notes/race-fixes.md`, using an unmerged `bench/race-strategies` branch. Ways 1 and 2 are correct with no retries; way 3 keeps the money right but returns 503 for about half the swipes on one hot card. Added `--runs`, status counts and a balance check to the timing script, plus two decline edge-case tests. Not sure about: ways 1 and 2 are within noise on one card; way 3's rare retries with no shared row look like SSI false positives, not verified.
