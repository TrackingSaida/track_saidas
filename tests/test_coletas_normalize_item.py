"""Coleta em lote: revalidação de código/serviço (alinhada a entrada/saída)."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret")

import pytest
from fastapi import HTTPException

from coletas import ItemLote, _normalize_item_coleta
from codigo_normalizer import normalize_codigo


@pytest.mark.parametrize(
    "codigo",
    ["00000030086", "00000030164", "12345678901", "00000000000"],
)
def test_numeros_fora_do_padrao_ml_sao_invalidos(codigo):
    """Códigos que o mobile antigo marcava como ML não passam no normalizer."""
    c, servico, _ = normalize_codigo(codigo, strict_qr=False)
    assert c is None
    assert servico is None

    with pytest.raises(HTTPException) as exc:
        _normalize_item_coleta(ItemLote(codigo=codigo, servico="Mercado Livre"))
    assert exc.value.status_code == 422
    assert "Código inválido" in str(exc.value.detail)


def test_ml_valido_mantem_mercado_livre_mesmo_com_servico_errado_do_cliente():
    item = _normalize_item_coleta(
        ItemLote(codigo="45000000001", servico="Avulso")
    )
    assert item.codigo == "45000000001"
    assert item.servico == "Mercado Livre"


def test_telefone_valido_vira_avulso_mesmo_marcado_como_ml():
    """Corrige classificação errada do mobile (\\d{10,} → Mercado Livre)."""
    item = _normalize_item_coleta(
        ItemLote(codigo="11948489168", servico="Mercado Livre")
    )
    assert item.codigo == "11948489168"
    assert item.servico == "Avulso"


def test_shopee_valido():
    item = _normalize_item_coleta(
        ItemLote(codigo="BR2656127018725", servico="Avulso")
    )
    assert item.codigo == "BR2656127018725"
    assert item.servico == "Shopee"
