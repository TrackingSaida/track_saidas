"""Filtro de campos só-entrega e validação de CRUD por contexto."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")

import pytest
from fastapi import HTTPException

from pedido_campos_obrigatorios_service import (
    filter_campos_for_contexto,
    normalize_campos_obrigatorios,
    resolve_campos_obrigatorios_from_cache,
)


def test_filter_ausente_remove_campos_so_entregue():
    campos = ["foto", "recebedor", "tipo_recebedor", "documento", "observacao"]
    assert filter_campos_for_contexto(campos, "AUSENTE") == ["foto", "observacao"]


def test_filter_entregue_mantem_todos():
    campos = ["foto", "recebedor", "tipo_recebedor", "documento", "observacao"]
    assert filter_campos_for_contexto(campos, "ENTREGUE") == campos


def test_resolve_from_cache_ambos_com_recebedor_nao_quebra_ausente():
    cache = {
        ("Shopee", "AMBOS"): ["recebedor", "tipo_recebedor", "foto"],
    }
    assert resolve_campos_obrigatorios_from_cache(
        cache=cache, servico="Shopee", contexto="AUSENTE"
    ) == ["foto"]
    assert resolve_campos_obrigatorios_from_cache(
        cache=cache, servico="Shopee", contexto="ENTREGUE"
    ) == ["foto", "recebedor", "tipo_recebedor"]


def test_normalize_rejeita_recebedor_em_ambos():
    with pytest.raises(HTTPException) as exc:
        normalize_campos_obrigatorios(["recebedor", "foto"], contexto="AMBOS")
    assert exc.value.status_code == 422
    assert "Recebedor" in str(exc.value.detail)


def test_normalize_permite_foto_em_ambos():
    assert normalize_campos_obrigatorios(["foto"], contexto="AMBOS") == ["foto"]


def test_normalize_permite_recebedor_em_entregue():
    assert normalize_campos_obrigatorios(
        ["recebedor", "tipo_recebedor"], contexto="ENTREGUE"
    ) == ["recebedor", "tipo_recebedor"]
