"""Caracterização do recálculo atual de coleta (PRD-017).

Congela o comportamento financeiro/operacional **antes** de qualquer mudança
do listener. Não altera o mecanismo em produção.
"""
from __future__ import annotations

import os
import sys
import types
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")


def _install_import_stubs() -> None:
    """Reduz a árvore de import só para exercitar as fórmulas."""

    def ensure(name: str, module: types.ModuleType | None = None) -> types.ModuleType:
        if name in sys.modules and not isinstance(sys.modules[name], MagicMock):
            # Já real — não sobrescrever.
            return sys.modules[name]
        mod = module or types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    auth = ensure("auth")
    auth.get_current_user = MagicMock()
    auth.User = MagicMock()

    saidas_routes = ensure("saidas_routes")
    saidas_routes._gerar_codigo_avulso = MagicMock(return_value="AV1")
    saidas_routes._normalizar_label_avulso = MagicMock(side_effect=lambda x: x)

    upload = ensure("upload_storage_utils")
    upload.create_presigned_upload = MagicMock()
    upload.create_presigned_get = MagicMock()
    upload.delete_object = MagicMock()
    upload.object_exists = MagicMock(return_value=False)

    # botocore/boto3 usados por upload_storage_utils se import real ocorrer
    ensure("boto3", types.ModuleType("boto3"))
    botocore = ensure("botocore", types.ModuleType("botocore"))
    client = ensure("botocore.client", types.ModuleType("botocore.client"))
    client.Config = MagicMock
    botocore.client = client
    ensure("botocore.exceptions", types.ModuleType("botocore.exceptions")).ClientError = type(
        "ClientError", (Exception,), {}
    )

    jose = ensure("jose", types.ModuleType("jose"))
    jose.JWTError = type("JWTError", (Exception,), {})
    jose.jwt = MagicMock()
    passlib = ensure("passlib", types.ModuleType("passlib"))
    ctx = ensure("passlib.context", types.ModuleType("passlib.context"))
    ctx.CryptContext = MagicMock
    passlib.context = ctx


_install_import_stubs()

from coletas import recalcular_coleta  # noqa: E402
from coleta_leituras_service import _recalcular_coleta_sem_commit  # noqa: E402


def _coleta(**overrides):
    values = {
        "id_coleta": 1,
        "sub_base": "SB",
        "base": "BASE",
        "origem": "codigo",
        "shopee": 0,
        "mercado_livre": 0,
        "avulso": 0,
        "pacotes_g": 0,
        "g_shopee": 0,
        "g_ml": 0,
        "g_avulso": 0,
        "valor_total": Decimal("0.00"),
        "execucao_id": None,
        "participante_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _saida(servico: str, *, is_grande: bool = False, id_saida: int = 1):
    return SimpleNamespace(
        id_saida=id_saida,
        id_coleta=1,
        servico=servico,
        is_grande=is_grande,
    )


def test_listener_formula_counts_servicos_and_ignores_is_grande():
    coleta = _coleta()
    saidas = [
        _saida("Shopee", is_grande=True, id_saida=1),
        _saida("Mercado Livre", id_saida=2),
        _saida("outro", id_saida=3),
    ]
    db = MagicMock()
    db.get.return_value = coleta
    db.scalars.return_value.all.return_value = saidas

    with patch(
        "coletas._get_precos_cached",
        return_value=(Decimal("2"), Decimal("3"), Decimal("5")),
    ):
        out = recalcular_coleta(db, 1, commit=False)

    assert out.shopee == 1
    assert out.mercado_livre == 1
    assert out.avulso == 1
    assert out.valor_total == Decimal("10.00")
    assert out.pacotes_g == 0


def test_listener_formula_empty_raises_400():
    coleta = _coleta()
    db = MagicMock()
    db.get.return_value = coleta
    db.scalars.return_value.all.return_value = []
    with pytest.raises(HTTPException) as exc:
        recalcular_coleta(db, 1, commit=False)
    assert exc.value.status_code == 400


def test_leituras_formula_updates_g():
    coleta = _coleta()
    saidas = [
        _saida("Shopee", is_grande=True, id_saida=1),
        _saida("Shopee", is_grande=False, id_saida=2),
        _saida("Mercado_Livre", is_grande=True, id_saida=3),
    ]
    db = MagicMock()
    db.scalars.return_value.all.return_value = saidas
    base_ref = SimpleNamespace(
        shopee=Decimal("2"), ml=Decimal("3"), avulso=Decimal("5")
    )

    with patch("coleta_leituras_service.resolver_base", return_value=base_ref):
        out = _recalcular_coleta_sem_commit(db, coleta)

    assert out is coleta
    assert out.shopee == 2
    assert out.mercado_livre == 1
    assert out.avulso == 0
    assert out.pacotes_g == 2
    assert out.g_shopee == 1
    assert out.g_ml == 1
    assert out.g_avulso == 0
    assert out.valor_total == Decimal("7.00")


def test_leituras_formula_empty_deletes_coleta():
    coleta = _coleta()
    db = MagicMock()
    db.scalars.return_value.all.return_value = []
    out = _recalcular_coleta_sem_commit(db, coleta)
    assert out is None
    db.delete.assert_called_once_with(coleta)


def test_formulas_diverge_on_pacote_g():
    coleta_a = _coleta()
    coleta_b = _coleta()
    saidas = [_saida("Shopee", is_grande=True)]
    db = MagicMock()
    db.get.return_value = coleta_a
    db.scalars.return_value.all.return_value = saidas
    prices = (Decimal("2"), Decimal("3"), Decimal("5"))
    base_ref = SimpleNamespace(
        shopee=Decimal("2"), ml=Decimal("3"), avulso=Decimal("5")
    )

    with patch("coletas._get_precos_cached", return_value=prices):
        listener = recalcular_coleta(db, 1, commit=False)
    with patch("coleta_leituras_service.resolver_base", return_value=base_ref):
        leituras = _recalcular_coleta_sem_commit(db, coleta_b)

    assert listener.valor_total == leituras.valor_total == Decimal("2.00")
    assert listener.pacotes_g == 0
    assert leituras.pacotes_g == 1
