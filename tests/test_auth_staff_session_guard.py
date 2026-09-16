"""Hotfix: JWT staff não opera se o cadastro for motoboy (role=4)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import auth as auth_mod


def _user(**kwargs):
    defaults = {
        "id": 1,
        "status": True,
        "role": 2,
        "password_hash": "hashed",
        "email": "a@b.com",
        "username": "user1",
        "contato": "11999999999",
        "sub_base": "BASE_A",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _ensure_staff_jwt_matches_db
# ---------------------------------------------------------------------------


def test_staff_jwt_rejected_when_db_role_is_motoboy(monkeypatch):
    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    db.get.return_value = _user(id=10, role=4, status=True)

    with pytest.raises(HTTPException) as exc:
        auth_mod._ensure_staff_jwt_matches_db(db, uid=10, jwt_role=2)
    assert exc.value.status_code == 401
    assert "Sessão inválida" in str(exc.value.detail)


def test_staff_jwt_rejected_when_inactive(monkeypatch):
    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    db.get.return_value = _user(id=10, role=2, status=False)

    with pytest.raises(HTTPException) as exc:
        auth_mod._ensure_staff_jwt_matches_db(db, uid=10, jwt_role=2)
    assert exc.value.status_code == 401


def test_staff_jwt_rejected_when_role_diverges(monkeypatch):
    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    db.get.return_value = _user(id=10, role=1, status=True)

    with pytest.raises(HTTPException) as exc:
        auth_mod._ensure_staff_jwt_matches_db(db, uid=10, jwt_role=2)
    assert exc.value.status_code == 401


def test_staff_jwt_accepted_when_role_matches(monkeypatch):
    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    db.get.return_value = _user(id=10, role=2, status=True)

    auth_mod._ensure_staff_jwt_matches_db(db, uid=10, jwt_role=2)


def test_staff_jwt_rejected_when_user_missing(monkeypatch):
    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    db.get.return_value = None

    with pytest.raises(HTTPException) as exc:
        auth_mod._ensure_staff_jwt_matches_db(db, uid=99, jwt_role=2)
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# authenticate_user — identificador ambíguo entre sub_bases
# ---------------------------------------------------------------------------


def test_authenticate_single_match(monkeypatch):
    user = _user(id=1, role=2, password_hash="h1")
    monkeypatch.setattr(auth_mod, "get_users_by_identifier", lambda _db, _id: [user])
    monkeypatch.setattr(auth_mod, "verify_password", lambda plain, hashed: hashed == "h1")

    assert auth_mod.authenticate_user(MagicMock(), "user1", "secret") is user


def test_authenticate_picks_matching_password_among_duplicates(monkeypatch):
    u_a = _user(id=1, username="dup", sub_base="A", password_hash="hash_a")
    u_b = _user(id=2, username="dup", sub_base="B", password_hash="hash_b")
    monkeypatch.setattr(auth_mod, "get_users_by_identifier", lambda _db, _id: [u_a, u_b])
    monkeypatch.setattr(
        auth_mod,
        "verify_password",
        lambda plain, hashed: hashed == "hash_b",
    )

    assert auth_mod.authenticate_user(MagicMock(), "dup", "secret") is u_b


def test_authenticate_rejects_ambiguous_same_password(monkeypatch):
    u_a = _user(id=1, username="dup", sub_base="A", password_hash="same")
    u_b = _user(id=2, username="dup", sub_base="B", password_hash="same")
    monkeypatch.setattr(auth_mod, "get_users_by_identifier", lambda _db, _id: [u_a, u_b])
    monkeypatch.setattr(auth_mod, "verify_password", lambda plain, hashed: True)

    assert auth_mod.authenticate_user(MagicMock(), "dup", "secret") is None


def test_authenticate_skips_inactive(monkeypatch):
    inactive = _user(id=1, status=False, password_hash="h")
    active = _user(id=2, status=True, password_hash="h")
    monkeypatch.setattr(auth_mod, "get_users_by_identifier", lambda _db, _id: [inactive, active])
    monkeypatch.setattr(auth_mod, "verify_password", lambda plain, hashed: True)

    assert auth_mod.authenticate_user(MagicMock(), "x", "secret") is active


def test_get_user_by_identifier_returns_none_when_ambiguous(monkeypatch):
    monkeypatch.setattr(
        auth_mod,
        "get_users_by_identifier",
        lambda _db, _id: [_user(id=1), _user(id=2)],
    )
    assert auth_mod.get_user_by_identifier(MagicMock(), "dup") is None


# ---------------------------------------------------------------------------
# get_current_user — integração leve com JWT staff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_user_staff_calls_db_guard(monkeypatch):
    called = {}

    def fake_ensure(db, *, uid, jwt_role):
        called["uid"] = uid
        called["jwt_role"] = jwt_role

    monkeypatch.setattr(auth_mod, "_ensure_staff_jwt_matches_db", fake_ensure)
    monkeypatch.setattr(
        auth_mod.jwt,
        "decode",
        lambda *a, **k: {
            "uid": 42,
            "role": 2,
            "owner_ativo": True,
            "username": "op",
            "ignorar_coleta": False,
        },
    )

    request = MagicMock()
    request.cookies.get.return_value = "fake.jwt.token"
    request.state = SimpleNamespace()

    user = await auth_mod.get_current_user(request, credentials=None, db=MagicMock())
    assert called == {"uid": 42, "jwt_role": 2}
    assert user.role == 2


@pytest.mark.asyncio
async def test_get_current_user_motoboy_skips_staff_guard(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("staff guard não deve rodar para role=4")

    monkeypatch.setattr(auth_mod, "_ensure_staff_jwt_matches_db", boom)
    monkeypatch.setattr(
        auth_mod.jwt,
        "decode",
        lambda *a, **k: {
            "uid": 7,
            "role": 4,
            "motoboy_id": 99,
            "owner_ativo": True,
            "username": "moto",
            "ignorar_coleta": False,
        },
    )

    request = MagicMock()
    request.cookies.get.return_value = "fake.jwt.token"
    request.state = SimpleNamespace()

    user = await auth_mod.get_current_user(request, credentials=None, db=MagicMock())
    assert user.role == 4
