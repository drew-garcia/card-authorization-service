"""The invariant checker: money rules that must hold however requests interleave.

check_invariants() reads the whole database and returns a description of
every rule that is broken, so an empty list means all is well. The
concurrency tests run it after a burst of random requests; it can also be
run against the dev database by hand.

The rules:

1. Every ledger transaction has at least two entries, and they sum to zero.
2. Every capture has exactly one ledger transaction, and it posts
   card_spend -X / merchant_settlement +X for the capture's amount.
3. For every card: available = limit - pending holds - captured, where
   captured comes from the ledger (minus the card's card_spend entries).
4. For every card, the ledger's captured total matches what its holds say
   was captured.

It uses plain SQL with GROUP BY: the database does the sums, so the check is
one query per rule however many rows there are. It only reads, so for a
consistent answer run it while nothing else is writing.
"""

from sqlalchemy import text
from sqlmodel import Session


def check_invariants(session: Session) -> list[str]:
    """Return one message per broken rule, or an empty list if all hold."""
    problems: list[str] = []

    unbalanced = session.execute(
        text(
            """
            SELECT t.id, COUNT(e.id), COALESCE(SUM(e.amount_cents), 0)
            FROM ledger_transactions t
            LEFT JOIN ledger_entries e ON e.ledger_transaction_id = t.id
            GROUP BY t.id
            HAVING COUNT(e.id) < 2 OR COALESCE(SUM(e.amount_cents), 0) <> 0
            """
        )
    ).all()
    for txn_id, count, total in unbalanced:
        problems.append(
            f"ledger transaction {txn_id} has {count} entries summing to {total}"
        )

    # For each capture: how many ledger transactions it has, and what they
    # post to each account. FILTER limits an aggregate to matching rows.
    bad_captures = session.execute(
        text(
            """
            SELECT c.id, c.amount_cents,
                   COUNT(DISTINCT t.id),
                   COALESCE(SUM(e.amount_cents)
                            FILTER (WHERE e.account = 'card_spend'
                                    AND e.card_id = h.card_id), 0),
                   COALESCE(SUM(e.amount_cents)
                            FILTER (WHERE e.account = 'merchant_settlement'), 0)
            FROM captures c
            JOIN holds h ON h.id = c.hold_id
            LEFT JOIN ledger_transactions t ON t.capture_id = c.id
            LEFT JOIN ledger_entries e ON e.ledger_transaction_id = t.id
            GROUP BY c.id, c.amount_cents
            HAVING COUNT(DISTINCT t.id) <> 1
                OR COALESCE(SUM(e.amount_cents)
                            FILTER (WHERE e.account = 'card_spend'
                                    AND e.card_id = h.card_id), 0)
                   <> -c.amount_cents
                OR COALESCE(SUM(e.amount_cents)
                            FILTER (WHERE e.account = 'merchant_settlement'), 0)
                   <> c.amount_cents
            """
        )
    ).all()
    for capture_id, amount, txns, card_side, merchant_side in bad_captures:
        problems.append(
            f"capture {capture_id} of {amount} cents has {txns} ledger "
            f"transactions posting card_spend {card_side} and "
            f"merchant_settlement {merchant_side}"
        )

    # Subqueries, not joins, for the two sums: joining holds and entries onto
    # cards at once would repeat each hold once per entry and inflate both.
    cards = session.execute(
        text(
            """
            SELECT c.id, c.limit_cents, c.available_cents,
                   COALESCE((SELECT SUM(h.amount_cents) FROM holds h
                             WHERE h.card_id = c.id AND h.status = 'pending'), 0),
                   COALESCE((SELECT -SUM(e.amount_cents) FROM ledger_entries e
                             WHERE e.card_id = c.id
                               AND e.account = 'card_spend'), 0),
                   COALESCE((SELECT SUM(h.captured_cents) FROM holds h
                             WHERE h.card_id = c.id), 0)
            FROM cards c
            """
        )
    ).all()
    for card_id, limit, available, held, ledger_captured, holds_captured in cards:
        expected = limit - held - ledger_captured
        if available != expected:
            problems.append(
                f"card {card_id}: available {available} != limit {limit} "
                f"- pending holds {held} - captured {ledger_captured} = {expected}"
            )
        if ledger_captured != holds_captured:
            problems.append(
                f"card {card_id}: ledger shows {ledger_captured} cents captured "
                f"but its holds show {holds_captured}"
            )

    return problems
