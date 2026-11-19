"""Shared FastAPI dependencies."""

from authz.store import CardStore

# One store for the whole process. Data is lost on restart, which is fine
# until Postgres arrives.
_store = CardStore()


def get_store() -> CardStore:
    """Hand the store to a route.

    Routes ask for it with Depends(get_store) instead of importing _store
    directly. That way tests can swap in a fresh store through
    app.dependency_overrides, and the Postgres version can later yield a
    session-backed store from here.
    """
    return _store
