"""Request and response models for the HTTP API."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Largest value a Postgres BIGINT can hold. Amounts above this could never be stored.
BIGINT_MAX = 2**63 - 1


class AuthorizationRequest(BaseModel):
    """A card network asking us to approve or decline a swipe."""

    # extra="forbid": unknown fields are a 422, not silently dropped.
    # str_strip_whitespace: trim strings before the length checks run.
    # frozen: validated requests can't be mutated afterwards.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    # Idempotency key from the network (hard rule 4).
    network_request_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    card_id: UUID
    # strict=True rejects floats, numeric strings and bools instead of coercing them.
    amount_cents: Annotated[int, Field(gt=0, le=BIGINT_MAX, strict=True)]
    currency: Literal["USD"]
    merchant_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    # A string, not an int: real MCCs have leading zeros (e.g. "0742").
    # [0-9] rather than \d, because \d also matches non-ASCII digits like "٤".
    merchant_category_code: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}$")]


class CardCreate(BaseModel):
    """Body for POST /cards: open a card with a starting balance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ge=0: a card may start empty, but never negative.
    initial_balance_cents: Annotated[int, Field(ge=0, le=BIGINT_MAX, strict=True)]


class Card(BaseModel):
    """A card as the API returns it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    available_cents: int


# Literal types give a closed set of values: pyright flags typos, and /docs
# lists the allowed strings in the response schema.
Decision = Literal["approved", "declined"]
DeclineReason = Literal["insufficient_funds", "card_not_found"]


class AuthorizationResponse(BaseModel):
    """Our answer to an AuthorizationRequest."""

    model_config = ConfigDict(frozen=True)

    authorization_id: UUID
    network_request_id: str
    card_id: UUID
    amount_cents: int
    decision: Decision
    # None when approved; set when declined.
    decline_reason: DeclineReason | None
