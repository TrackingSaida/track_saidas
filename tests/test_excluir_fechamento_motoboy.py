"""Exclusão de fechamento de motoboy ainda não pago."""

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret")

# O gerador de PDF puxa reportlab na importação da rota. O teste não gera PDF.
for _mod in (
    "reportlab",
    "reportlab.lib",
    "reportlab.lib.colors",
    "reportlab.lib.pagesizes",
    "reportlab.lib.units",
    "reportlab.lib.utils",
    "reportlab.pdfgen",
    "reportlab.pdfgen.canvas",
):
    sys.modules.setdefault(_mod, MagicMock())

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from entregador_fechamento_routes import excluir_fechamento


def _fech(**overrides):
    base = {"id_fechamento": 9, "sub_base": "SB", "status": "GERADO"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _chamar(fech):
    db = MagicMock()
    db.get.return_value = fech
    with patch("entregador_fechamento_routes._resolve_user_base", return_value="SB"):
        excluir_fechamento(9, db, SimpleNamespace())
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


def test_excluir_pago_recusa():
    with pytest.raises(HTTPException) as exc:
        _chamar(_fech(status="PAGO"))
    assert exc.value.status_code == 400
    assert "pago" in str(exc.value.detail).lower()


def test_excluir_de_outra_sub_base_nao_encontra():
    with pytest.raises(HTTPException) as exc:
        _chamar(_fech(sub_base="OUTRA"))
    assert exc.value.status_code == 404
