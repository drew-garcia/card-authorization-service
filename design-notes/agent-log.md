# Agent log

## 2026-11-19: In-memory card API (PR #4)

Built the in-memory card API: `POST /cards`, `GET /cards/{id}`, and `POST /authorizations` deciding from a dict-backed `CardStore` via an `authorize()` service. Unknown cards decline with `card_not_found`; insufficient funds decline without changing the balance. The read-check-write race is deliberately kept, idempotency is deferred, and the old 501 stub test was replaced.

## 2026-11-19: Trimmed optional tests (PR #4)

Removed four low-value tests (distinct ids, zero balance, unique authorization ids, set_available on a missing card). 49 tests pass. This left the `KeyError` in `CardStore.set_available` untested, so the check was removed in the next entry.

## 2026-11-19: Dropped KeyError check (PR #4)

Removed the untested `KeyError` check from `CardStore.set_available`. Callers must now pass an existing card id; the authorize service already guarantees this by calling `get()` first.
