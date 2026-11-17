"""FastAPI application entry point."""

from fastapi import FastAPI

from authz.api import authorizations

app = FastAPI(title="Card authorization service")
app.include_router(authorizations.router)
