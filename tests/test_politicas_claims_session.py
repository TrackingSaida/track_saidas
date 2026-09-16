"""Políticas gerais: mapeamento coleta_habilitada ↔ ignorar_coleta + defaults."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from politicas_routes import _owner_to_out


def test_coleta_habilitada_inverte_ignorar_coleta():
    owner = SimpleNamespace(
        ignorar_coleta=True,
        modo_operacao="codigo",
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        devolucao_sub_base_habilitada=False,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    out = _owner_to_out(owner)
    assert out.operacao.coleta_habilitada is False

    owner.ignorar_coleta = False
    out2 = _owner_to_out(owner)
    assert out2.operacao.coleta_habilitada is True


def test_entrada_api_mapeia_coluna_legada():
    owner = SimpleNamespace(
        ignorar_coleta=False,
        modo_operacao="ambos",
        entrada_obrigatoria_habilitada=True,
        conferencia_saida_habilitada=True,
        devolucao_sub_base_habilitada=False,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    out = _owner_to_out(owner)
    assert out.operacao.entrada_habilitada is True
    assert out.operacao.conferencia_saida_habilitada is True
    assert out.operacao.modo_operacao == "ambos"


def test_padroes_motoboy_oficiais():
    owner = SimpleNamespace(
        ignorar_coleta=False,
        modo_operacao="codigo",
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        devolucao_sub_base_habilitada=False,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    pad = _owner_to_out(owner).padroes_motoboy
    assert pad.pode_realizar_coleta is False
    assert pad.pode_ler_saida is True
    assert pad.pode_digitar_codigo_manual is False
    assert pad.pode_lancar_avulso is True
    assert pad.avulso_exige_foto is True


def test_motoboy_access_ttl_default_2h():
    import auth as auth_mod

    assert auth_mod.MOTOBOY_ACCESS_TOKEN_EXPIRE_HOURS == 2
    delta = auth_mod._motoboy_access_expires()
    assert delta == timedelta(hours=2)


def test_idle_timeout_rejects_stale_activity(monkeypatch):
    import auth as auth_mod

    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    monkeypatch.setattr(auth_mod, "MOTOBOY_IDLE_TIMEOUT_DAYS", 7)
    monkeypatch.setattr(auth_mod, "MOTOBOY_REFRESH_ABSOLUTE_DAYS", 60)

    now = datetime.utcnow()
    row = SimpleNamespace(
        user_id=1,
        motoboy_id=2,
        expires_at=now + timedelta(days=30),
        created_at=now - timedelta(days=10),
        last_activity_at=now - timedelta(days=8),
        revoked_at=None,
    )
    db = MagicMock()
    db.scalar.return_value = row

    with pytest.raises(HTTPException) as exc:
        auth_mod._rotate_motoboy_refresh_token(db, "token-fake")
    assert exc.value.status_code == 401
    assert "inatividade" in str(exc.value.detail).lower()
    assert row.revoked_at is not None


def test_absolute_ceiling_rejects(monkeypatch):
    import auth as auth_mod

    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    monkeypatch.setattr(auth_mod, "MOTOBOY_IDLE_TIMEOUT_DAYS", 7)
    monkeypatch.setattr(auth_mod, "MOTOBOY_REFRESH_ABSOLUTE_DAYS", 60)

    now = datetime.utcnow()
    row = SimpleNamespace(
        user_id=1,
        motoboy_id=2,
        expires_at=now + timedelta(days=1),
        created_at=now - timedelta(days=61),
        last_activity_at=now,
        revoked_at=None,
    )
    db = MagicMock()
    db.scalar.return_value = row

    with pytest.raises(HTTPException) as exc:
        auth_mod._rotate_motoboy_refresh_token(db, "token-fake")
    assert exc.value.status_code == 401
    assert row.revoked_at is not None


def test_hydrate_sets_stale_header_when_version_diverges(monkeypatch):
    import auth as auth_mod
    from starlette.responses import Response

    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())

    motoboy = SimpleNamespace(
        id_motoboy=9,
        pode_realizar_coleta=False,
        pode_ler_saida=True,
        pode_digitar_codigo_manual=False,
        pode_lancar_avulso=True,
        avulso_exige_foto=True,
        claims_version=5,
        sub_base="BASE_X",
    )
    owner = SimpleNamespace(
        ignorar_coleta=False,
        modo_operacao="codigo",
        devolucao_sub_base_habilitada=False,
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        ativo=True,
        sub_base="BASE_X",
        valor=1,
        tipo_owner="subbase",
    )
    user = SimpleNamespace(id=42, sub_base="BASE_X", role=4)

    db = MagicMock()
    db.scalar.side_effect = [motoboy, owner]
    response = Response()

    out = auth_mod._hydrate_motoboy_permissions_from_db(
        db, user, response=response, jwt_claims_version=3
    )
    assert out.claims_version == 5
    assert response.headers.get(auth_mod.CLAIMS_STALE_HEADER) == "1"


def test_conferencia_nao_e_gate_de_saida():
    """Conferência é só informativa — helper de saída não a consulta."""
    from saida_prerequisito import avaliar_prerequisito_saida
    import inspect

    sig = inspect.signature(avaliar_prerequisito_saida)
    assert "conferencia" not in sig.parameters
