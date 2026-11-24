"""FastAPI application entry point."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from authz.api import authorizations, cards
from authz.services.authorizations import AuthorizationContendedError

app = FastAPI(title="Card authorization service")
app.include_router(cards.router)
app.include_router(authorizations.router)


@app.exception_handler(AuthorizationContendedError)
def _contended(_request: Request, _exc: AuthorizationContendedError) -> JSONResponse:
    """Retries ran out: nothing was written, so the network may retry."""
    return JSONResponse(
        status_code=503,
        content={"detail": "card busy, retry"},
        headers={"Retry-After": "0", "X-Authz-Retries": "3"},
    )
