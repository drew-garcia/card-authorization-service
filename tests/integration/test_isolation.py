"""Each test starts with empty tables.

The two tests are identical on purpose: whichever runs second would see two
cards if rows leaked from one test into the next.
"""

from collections.abc import Callable

from sqlmodel import Session, select

from authz.models import AuthorizationRow, CardRow

MakeCard = Callable[[int], str]


def assert_one_card_and_no_authorizations(session: Session) -> None:
    assert len(session.exec(select(CardRow)).all()) == 1
    assert session.exec(select(AuthorizationRow)).all() == []


def test_isolation_first(session: Session, make_card: MakeCard) -> None:
    make_card(500)
    assert_one_card_and_no_authorizations(session)


def test_isolation_second(session: Session, make_card: MakeCard) -> None:
    make_card(500)
    assert_one_card_and_no_authorizations(session)
