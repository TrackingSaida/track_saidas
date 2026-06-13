"""Testes de reconhecimento de códigos avulsos gerados (AVULSO-*)."""
from codigo_normalizer import (
    _is_codigo_avulso_gerado,
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
