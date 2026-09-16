"""Testes: pré-requisito Coleta OU Entrada + sessão/claims helpers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from saida_prerequisito import avaliar_prerequisito_saida


def test_matriz_ambos_off_sem_gate():
    assert (
        avaliar_prerequisito_saida(
            coleta_habilitada=False,
            entrada_habilitada=False,
            saida_existe=False,
        )
        is None
    )


def test_so_coleta_inexistente():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=True,
        entrada_habilitada=False,
        saida_existe=False,
    )
    assert err and err["code"] == "NAO_COLETADO"


def test_so_entrada_inexistente():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=False,
        entrada_habilitada=True,
        saida_existe=False,
    )
    assert err and err["code"] == "ENTRADA_OBRIGATORIA"


def test_ambos_on_inexistente():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=True,
        entrada_habilitada=True,
        saida_existe=False,
    )
    assert err and err["code"] == "PRE_REQUISITO_SAIDA"


def test_ambos_on_coletado_libera_or():
    """Antes era AND (bloqueava); agora OR libera com só coleta."""
    assert (
        avaliar_prerequisito_saida(
            coleta_habilitada=True,
            entrada_habilitada=True,
            saida_existe=True,
            status_norm="coletado",
        )
        is None
    )


def test_ambos_on_na_base_libera():
    assert (
        avaliar_prerequisito_saida(
            coleta_habilitada=True,
            entrada_habilitada=True,
            saida_existe=True,
            status_norm="NA_BASE",
        )
        is None
    )


def test_so_entrada_coletado_bloqueia():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=False,
        entrada_habilitada=True,
        saida_existe=True,
        status_norm="coletado",
    )
    assert err and err["code"] == "ENTRADA_OBRIGATORIA"


def test_so_coleta_coletado_ok():
    assert (
        avaliar_prerequisito_saida(
            coleta_habilitada=True,
            entrada_habilitada=False,
            saida_existe=True,
            status_norm="coletado",
        )
        is None
    )


def test_ja_em_rota_ignora_gate():
    assert (
        avaliar_prerequisito_saida(
            coleta_habilitada=True,
            entrada_habilitada=True,
            saida_existe=True,
            status_norm="coletado",
            ja_em_rota_ou_saida=True,
        )
        is None
    )


def test_staff_jwt_rejected_when_db_motoboy(monkeypatch):
    import auth as auth_mod

    monkeypatch.setattr(auth_mod, "run_db_query_with_retry", lambda _db, fn: fn())
    db = MagicMock()
    db.get.return_value = SimpleNamespace(id=10, role=4, status=True)
    with pytest.raises(HTTPException) as exc:
        auth_mod._ensure_staff_jwt_matches_db(db, uid=10, jwt_role=2)
    assert exc.value.status_code == 401
