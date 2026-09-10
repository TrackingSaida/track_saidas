"""Testes do helper qr_payload_utils."""
from __future__ import annotations

from types import SimpleNamespace

from qr_payload_utils import (
    apply_qr_payload_if_needed,
    has_usable_qr_etiqueta,
    is_ml_qr_completo,
    needs_qr_update,
    should_store_qr_payload_raw,
)

JSON_COMPLETO = '{"id":"45123456789","sender_id":123,"hash_code":"abc"}'
DIGITOS = "45123456789"


def test_should_store_json_completo():
    assert should_store_qr_payload_raw("Mercado Livre", JSON_COMPLETO) is True


def test_should_store_digitos():
    assert should_store_qr_payload_raw("Mercado Livre", DIGITOS) is True


def test_should_not_store_shopee():
    assert should_store_qr_payload_raw("Shopee", JSON_COMPLETO) is False


def test_is_ml_qr_completo():
    assert is_ml_qr_completo(JSON_COMPLETO) is True
    assert is_ml_qr_completo(DIGITOS) is False
    assert is_ml_qr_completo(None) is False


def test_needs_update_vazio():
    saida = SimpleNamespace(servico="Mercado Livre", qr_payload_raw=None)
    assert needs_qr_update(saida, DIGITOS) is True
    assert needs_qr_update(saida, JSON_COMPLETO) is True


def test_needs_update_fraco_para_completo():
    saida = SimpleNamespace(servico="Mercado Livre", qr_payload_raw=DIGITOS)
    assert needs_qr_update(saida, JSON_COMPLETO) is True
    assert needs_qr_update(saida, "45999888777") is False


def test_needs_update_nao_sobrescreve_completo():
    saida = SimpleNamespace(servico="Mercado Livre", qr_payload_raw=JSON_COMPLETO)
    assert needs_qr_update(saida, DIGITOS) is False
    assert needs_qr_update(saida, '{"id":"45999888777","sender_id":1,"hash_code":"x"}') is False


def test_apply_atualiza_e_alerta():
    saida = SimpleNamespace(servico="Mercado Livre", qr_payload_raw=None)
    r = apply_qr_payload_if_needed(saida, None)
    assert r["updated"] is False
    assert r["alerta"] is True

    r2 = apply_qr_payload_if_needed(saida, DIGITOS)
    assert r2["updated"] is True
    assert saida.qr_payload_raw == DIGITOS
    assert r2["alerta"] is True  # dígitos sem JSON completo

    r3 = apply_qr_payload_if_needed(saida, JSON_COMPLETO)
    assert r3["updated"] is True
    assert saida.qr_payload_raw == JSON_COMPLETO
    assert r3["alerta"] is False


def test_has_usable():
    assert has_usable_qr_etiqueta(JSON_COMPLETO) is True
    assert has_usable_qr_etiqueta(DIGITOS) is True
    assert has_usable_qr_etiqueta(None) is False
