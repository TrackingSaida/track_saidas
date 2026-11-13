"""Exclusão de fechamento de coleta ainda não recebido."""

import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret")


def _modulo(nome):
    if nome not in sys.modules:
        sys.modules[nome] = types.ModuleType(nome)
    return sys.modules[nome]


_modulo("boto3")
_modulo("botocore")
_modulo("botocore.client").Config = object
_modulo("botocore.exceptions")

import pytest
from fastapi import HTTPException

from base_fechamento_routes import excluir_fechamento


def _fech(**overrides):
    base = {"id_fechamento": 9, "sub_base": "SB", "status": "GERADO"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _chamar(fech, role=1):
    db = MagicMock()
    db.get.return_value = fech
    usuario = SimpleNamespace(role=role)
    with patch("base_fechamento_routes._sub_base_from_token_or_422", return_value="SB"):
        excluir_fechamento(9, db, usuario)
    return db


def test_excluir_gerado_apaga_o_fechamento():
    db = _chamar(_fech())
    db.delete.assert_called_once()
    db.commit.assert_called_once()


def test_excluir_reajustado_apaga_o_fechamento():
    db = _chamar(_fech(status="REAJUSTADO"))
    db.delete.assert_called_once()
    db.commit.assert_called_once()


def test_excluir_legado_fechado_apaga_o_fechamento():
    db = _chamar(_fech(status="FECHADO"))
    db.delete.assert_called_once()
    db.commit.assert_called_once()


def test_excluir_recebido_recusa():
    with pytest.raises(HTTPException) as exc:
        _chamar(_fech(status="RECEBIDO"))
    assert exc.value.status_code == 400
    assert "recebido" in str(exc.value.detail).lower()


def test_excluir_de_outra_sub_base_nao_encontra():
    with pytest.raises(HTTPException) as exc:
        _chamar(_fech(sub_base="OUTRA"))
    assert exc.value.status_code == 404


def test_excluir_sem_perfil_financeiro_recusa():
    with pytest.raises(HTTPException) as exc:
        _chamar(_fech(), role=2)
    assert exc.value.status_code == 403
