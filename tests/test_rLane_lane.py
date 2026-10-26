"""The routine lane (yuki.memory.routine_offers; PLAN.md Phase R, D10, D11, D14; B.3 lane tests): the offer-now
request file. Temp databases, an invented weekday, no model.

r30: the lane's card tests were cut. Each ran a routine on the hand-written ``meet.open`` skill (gone since lSplit,
10 Oct; routine cards are offered only for learned skills since lWire) through a coach worker built with
``media_fn`` / ``render_fn`` (gone since kProactive, 18 Oct). A rewrite would need a learned skill row and today's card
judge.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from _rlane_world import add_routine, open_store
from yuki.memory import routine_offers


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = open_store(tmp)
        try:
            yield s
        finally:
            s.close()


# -- offer-now: a request file ----------------------------------------------------------------------------------------


def test_offer_now_cli_writes_the_request_only(store, capsys):
    rid = add_routine(store)
    assert routine_offers.main(["offer-now", str(rid), "--in", "30", "--db", str(store.path)]) == 0
    req = routine_offers.read_request(store.path)
    assert req["routine_id"] == rid and req["state"] == "pending" and req["at"] - req["made_at"] == pytest.approx(30)
    assert routine_offers.main(["status", "--db", str(store.path)]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["state"] == "pending"
