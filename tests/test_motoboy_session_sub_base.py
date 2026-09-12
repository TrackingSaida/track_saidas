"""Resolução de sub_base de sessão do motoboy (anti cross-tenant)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from auth import _resolve_motoboy_session_sub_base


def test_resolve_ignores_stale_preferred_when_single_link(monkeypatch):
    """JWT/users.sub_base=WS sem vínculo → usa único MotoboySubBase (RUB_TEST1)."""
    import auth as auth_mod

    db = MagicMock()
    monkeypatch.setattr(
        auth_mod,
        "run_db_query_with_retry",
        lambda _db, fn: fn(),
    )
    result = MagicMock()
    result.all.return_value = ["RUB_TEST1"]
    db.scalars.return_value = result

    user = SimpleNamespace(sub_base="WS")
    motoboy = SimpleNamespace(id_motoboy=10)

    assert _resolve_motoboy_session_sub_base(db, user=user, motoboy=motoboy) == "RUB_TEST1"


def test_resolve_keeps_preferred_when_linked(monkeypatch):
    import auth as auth_mod

    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = ["RUB_TEST1", "WS"]
    db.scalars.return_value = result

    user = SimpleNamespace(sub_base="WS")
    motoboy = SimpleNamespace(id_motoboy=10)

    assert _resolve_motoboy_session_sub_base(db, user=user, motoboy=motoboy) == "WS"


def test_resolve_raises_when_no_links(monkeypatch):
    import auth as auth_mod

    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    db.scalars.return_value = result

    with pytest.raises(HTTPException) as exc:
        _resolve_motoboy_session_sub_base(
            db,
            user=SimpleNamespace(sub_base="WS"),
            motoboy=SimpleNamespace(id_motoboy=10),
        )
    assert exc.value.status_code == 403
