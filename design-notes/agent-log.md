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

## 2026-11-24: Idempotent authorizations (PR #11)

Added a unique constraint on `network_request_id`, a `request_hash` column (SHA-256 of the validated body), and `INSERT ... ON CONFLICT DO NOTHING` after the conditional UPDATE. On a repeat it rolls back (undoing the debit): same hash returns the original response, different hash returns 409. 50 identical concurrent requests create 1 row and get identical responses, 20/20 runs. Also fixed `live_server`: an unread stdout pipe filled up and froze the workers, and a shutdown timeout left orphaned workers holding DB connections. Not sure about: option A (debit, then insert) makes repeats take the card lock briefly; claiming the id first may fit better once holds and the ledger exist.

## 2026-11-24: Dev database reset and migrated (PR #11)

Reset the dev database (`alembic downgrade base`, dropping 155 cards and 350 authorizations of timing data), then `alembic upgrade head` to `daa7aa521fce`; `alembic check` is clean. Checked by hand against a live server: a repeat returns the original response and debits once, and a different body returns 409. It left one test card and authorization (`manual-1`) in the dev database.

## 2026-11-27: Double-entry ledger for captures

Added `ledger_transactions` and `ledger_entries` (signed BIGINT cents) with a migration that backfills existing captures. Each capture now posts card_spend −X / merchant_settlement +X inside its own transaction, through `services/ledger.py`, which checks the entries balance before inserting. A deferred constraint trigger refuses any commit where a transaction's entries don't sum to zero. Added `authz/invariants.py` (`check_invariants`) and a concurrency test that runs 1,000 seeded random operations in 10 waves of 100. It found no violations in 30/30 runs. It also failed as expected when posting was moved after the commit, and when the hold amount was posted instead of the captured amount. 167 tests, ruff, pyright and `alembic check` pass (run locally against Postgres 16, since Docker wasn't available). Not sure about: ledger posting is owner-core code, written on explicit request, so it needs your review. Merchants aren't stored, so all settlement goes to one shared `merchant_settlement` account. Bursts of requests exhausted Postgres's 100 connections (fixed in the next entry).

## 2026-11-28: Thread-safe engine and fixed-size pool (PR #14 CI fix)

CI failed on PR #14: the new ledger concurrency test got 124 `500`s from Postgres `too many clients already` before reaching the ledger checks. Tagging each worker's connections with `application_name` showed one worker holding 44 connections. Cause: `get_engine()` used `@lru_cache`, which doesn't lock while building, so a worker's first burst of threadpool requests each built their own engine and pool. A unit test with a `threading.Barrier` reproduced it (4–24 engines from 50 threads). Fixed with a double-checked lock in `db.get_engine()` (plus `dispose_engine()` for tests), and made the pool fixed-size (`DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=0`) so bursts queue instead of opening and closing extra connections. Peak connections under 250-request bursts dropped from 84–100 to 42. 10/10 runs pass at that size, and the old settings failed 1 in 3. 170 tests, ruff and pyright pass. Not sure about: `pool_timeout` stays at the default 30 s, so a long queue for a connection would now be slow rather than a fast 500. Worth revisiting with k6.

## 2026-11-28: Hold expiry worker

Added `expire_holds()` in `services/holds.py`: one transaction claims up to 100 pending holds older than `HOLD_EXPIRY_SECONDS` (default 7 days) with `FOR UPDATE SKIP LOCKED`, marks them expired (guarded by `status = 'pending'`) and adds the released cents back to each card in sorted card-id order. `workers/hold_expiry.py` runs it every 60 s (`uv run authz-hold-expiry`), draining full batches, stopping cleanly on SIGTERM, and a partial index on pending holds' `created_at` keeps the claim fast. 4 concurrent workers draining 1,000 holds released each exactly once in 20/20 runs, and so did expiry racing 250 live captures and reversals (split about half and half). Removing the lock and the status guard made it release 2,860 holds' worth of money. 195 tests, ruff, pyright and `alembic check` pass (local Postgres 16). Not sure about: this is owner-core code (row locks and balance changes), written on explicit request, so it needs your review. Expiry writes no ledger or audit row, and a capture after expiry gets 409 (no late or forced captures). The 7-day default is one window for every merchant type, while real networks vary it by MCC.
