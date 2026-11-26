"""FastAPI application entry point."""

from fastapi import FastAPI

from authz.api import authorizations, cards, holds

app = FastAPI(title="Card authorization service")
app.include_router(cards.router)
app.include_router(authorizations.router)
app.include_router(holds.router)
