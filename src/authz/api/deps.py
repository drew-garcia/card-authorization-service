"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends
from sqlmodel import Session

from authz.db import get_session

# Routes declare `session: SessionDep` to get one database session per request.
# Tests swap the session in through app.dependency_overrides[get_session].
SessionDep = Annotated[Session, Depends(get_session)]
