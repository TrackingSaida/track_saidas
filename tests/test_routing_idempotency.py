"""Idempotência: retry tardio de A após B não reexecuta A."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from routing.idempotency import (
    STATUS_COMPLETED,
    begin_idempotent_request,
    complete_idempotent_request,
    find_by_idempotency_key,
)


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def one_or_none(self):
        return self._row


def test_idempotency_replay_after_later_operation():
    """
    A executa com key A
    B executa com key B
    retry tardio de A → replay de A, sem nova execução
    """
    row_a = SimpleNamespace(
        status=STATUS_COMPLETED,
        response_json=json.dumps({"ordem": [1, 2], "modo": "google", "optimization_mode": "google"}),
        idempotency_key="key-A",
    )
    db = MagicMock()
    db.query.return_value = _FakeQuery(row_a)

    action, cached, row = begin_idempotent_request(
        db,
        sub_base="base1",
        motoboy_id=7,
        idempotency_key="key-A",
    )
    assert action == "replay"
    assert cached["ordem"] == [1, 2]
    assert row is row_a


def test_idempotency_in_progress():
    row = SimpleNamespace(status="pending", response_json=None, idempotency_key="k")
    db = MagicMock()
    db.query.return_value = _FakeQuery(row)
    action, cached, _ = begin_idempotent_request(
        db, sub_base="b", motoboy_id=1, idempotency_key="k"
    )
    assert action == "in_progress"
    assert cached is None
