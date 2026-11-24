"""Time N concurrent authorizations against a running server and print p50/p99.

Start the server first, for example:

    uv run uvicorn authz.main:app --workers 4

Then:

    uv run python scripts/time_authorizations.py                     # all swipes on one card
    uv run python scripts/time_authorizations.py --card-per-request  # one card per swipe

With one card, every swipe waits for the same row lock, so this is the worst
case for contention. With a card per swipe, the locks never conflict, which
shows the cost of everything else in the request.
"""

import argparse
import asyncio
import statistics
import time
from uuid import uuid4

import httpx

AMOUNT_CENTS = 1_000  # $10


async def _create_card(client: httpx.AsyncClient, balance_cents: int) -> str:
    response = await client.post(
        "/cards", json={"initial_balance_cents": balance_cents}
    )
    response.raise_for_status()
    return response.json()["id"]


async def _timed_swipe(
    client: httpx.AsyncClient, card_id: str
) -> tuple[float, httpx.Response]:
    body = {
        # uuid4 keeps ids unique across runs, since the script doesn't clean the db.
        "network_request_id": f"timing-{uuid4()}",
        "card_id": card_id,
        "amount_cents": AMOUNT_CENTS,
        "currency": "USD",
        "merchant_name": "Corner Coffee",
        "merchant_category_code": "5814",
    }
    # perf_counter is a monotonic, high-resolution clock meant for timing.
    start = time.perf_counter()
    response = await client.post("/authorizations", json=body)
    return time.perf_counter() - start, response


async def run(base_url: str, requests: int, card_per_request: bool) -> None:
    # The default pool allows 100 connections; size it so no request waits
    # for a client-side connection and skews the timing.
    limits = httpx.Limits(max_connections=requests)
    async with httpx.AsyncClient(base_url=base_url, timeout=30, limits=limits) as c:
        # Enough balance that every swipe is approved: we time the lock, not declines.
        if card_per_request:
            card_ids = [await _create_card(c, AMOUNT_CENTS) for _ in range(requests)]
        else:
            card_ids = [await _create_card(c, AMOUNT_CENTS * requests)] * requests
        results = await asyncio.gather(*(_timed_swipe(c, cid) for cid in card_ids))

    latencies_ms = sorted(seconds * 1000 for seconds, _ in results)
    approved = sum(r.json().get("decision") == "approved" for _, r in results)
    # quantiles(n=100) returns the 99 cut points between percentiles 1..99,
    # so index 49 is p50 and index 98 is p99.
    cuts = statistics.quantiles(latencies_ms, n=100, method="inclusive")
    mode = "one card per request" if card_per_request else "all on one card"
    print(f"{requests} concurrent requests ({mode}), {approved} approved")
    print(
        f"p50 {cuts[49]:.1f} ms   p99 {cuts[98]:.1f} ms   max {latencies_ms[-1]:.1f} ms"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Time concurrent authorizations.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--card-per-request", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.requests, args.card_per_request))


if __name__ == "__main__":
    main()
