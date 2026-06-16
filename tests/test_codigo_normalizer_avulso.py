"""Testes de classificação de códigos (Shopee, Avulso, rejeição)."""
from codigo_normalizer import (
    _is_codigo_avulso_gerado,
    _is_telefone_brasil,
    _normalize_shopee_codigo,
    is_qr_like_scan_payload,
    normalize_codigo,
)


def test_avulso_gerado_com_label():
    codigo = "AVULSO-9JULHO-000019"
    assert _is_codigo_avulso_gerado(codigo)
    assert is_qr_like_scan_payload(codigo)
    c, servico, qr_raw = normalize_codigo(codigo, strict_qr=True)
    assert c == codigo
    assert servico == "Avulso"
    assert qr_raw is None


def test_avulso_gerado_sem_label():
    codigo = "AVULSO-000019"
    assert _is_codigo_avulso_gerado(codigo)
    c, servico, _ = normalize_codigo(codigo, strict_qr=True)
    assert c == codigo
    assert servico == "Avulso"


def test_strict_qr_rejeita_lixo():
    c, servico, qr_raw = normalize_codigo("texto invalido xyz", strict_qr=True)
    assert c is None
    assert servico is None
    assert qr_raw is None


def test_shopee_com_prefixo_br():
    c, servico, _ = normalize_codigo("BR2656127018725", strict_qr=False)
    assert c == "BR2656127018725"
    assert servico == "Shopee"


def test_shopee_13_digitos_sem_prefixo():
    c, servico, _ = normalize_codigo("2656127018725", strict_qr=False)
    assert c == "BR2656127018725"
    assert servico == "Shopee"


def test_shopee_truncado_11_digitos_nao_vira_avulso():
    c, servico, _ = normalize_codigo("26561280188", strict_qr=False)
    assert c is None
    assert servico is None


def test_shopee_truncado_nao_vira_telefone():
    assert _is_telefone_brasil("26561280188") is None


def test_telefone_valido_celular():
    c, servico, _ = normalize_codigo("(11) 94848-9168", strict_qr=False)
    assert c == "11948489168"
    assert servico == "Avulso"


def test_telefone_com_ddd_invalido_rejeitado():
    c, servico, _ = normalize_codigo("26561280188", strict_qr=False)
    assert servico is None


def test_cep_nao_e_mais_avulso():
    c, servico, _ = normalize_codigo("06447380", strict_qr=False)
    assert c is None
    assert servico is None


def test_lm_nao_e_mais_avulso():
    c, servico, _ = normalize_codigo("LM12345", strict_qr=False)
    assert c is None
    assert servico is None


def test_normalize_shopee_embedded():
    assert _normalize_shopee_codigo("XBR2656127018725Y", "2656127018725") == "BR2656127018725"


def test_is_qr_like_shopee_digits():
    assert is_qr_like_scan_payload("2656127018725") is True


def test_is_qr_like_rejeita_truncado():
    assert is_qr_like_scan_payload("26561280188") is False
