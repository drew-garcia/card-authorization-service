"""Helpers shared by every operation keyed on a network ID (hard rule 4)."""

import hashlib
import json

from pydantic import BaseModel


class IdempotencyConflict(Exception):
    """A network ID was reused with a different request body."""


def request_hash(request: BaseModel) -> str:
    """SHA-256 hex of the validated request, used to tell a retry from a reuse.

    It hashes the parsed model, not the raw bytes, so JSON key order and the
    whitespace Pydantic strips don't count as a different body. sort_keys and
    fixed separators make the JSON text canonical; mode="json" turns UUIDs
    into strings so json.dumps can handle them.

    It takes any BaseModel, so authorizations, captures and reversals share it.
    """
    canonical = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
