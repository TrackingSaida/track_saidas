"""Tipo da chave PIX a receber e a linha do relatório."""
import pytest

from fechamento_base_ciclo_pure import (
    aplicar_chave_pix,
    resolver_pix_receber,
    texto_pix_relatorio,
)


CPF = "52998224725"


def _cnpj_valido_conhecido() -> str:
    """CNPJ com dígitos calculados, para não depender de um número memorizado."""
    base = "112223330001"
    pesos_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos_2 = [6, *pesos_1]

    def dv(nums: str, pesos: list) -> str:
        resto = sum(int(n) * p for n, p in zip(nums, pesos)) % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = dv(base, pesos_1)
    d2 = dv(base + d1, pesos_2)
    return base + d1 + d2


def test_cpf_e_cnpj_guardam_so_os_digitos():
    cnpj = _cnpj_valido_conhecido()
    assert aplicar_chave_pix("cpf", "529.982.247-25", None, None) == ("cpf", CPF)
    assert aplicar_chave_pix("CNPJ", cnpj[:2] + "." + cnpj[2:], None, None) == ("cnpj", cnpj)
    with pytest.raises(ValueError, match="CPF inválido"):
        aplicar_chave_pix("cpf", "111.111.111-11", None, None)


def test_celular_email_e_aleatoria():
    assert aplicar_chave_pix("telefone", "+55 (11) 98765-4321", None, None) == ("telefone", "+5511987654321")
    assert aplicar_chave_pix("email", " Financeiro@Empresa.com ", None, None) == ("email", "financeiro@empresa.com")
    chave = "123E4567-E89B-12D3-A456-426614174000"
    assert aplicar_chave_pix("aleatoria", chave, None, None) == ("aleatoria", chave.lower())
    with pytest.raises(ValueError, match="E-mail inválido"):
        aplicar_chave_pix("email", "a" * 72 + "@b.com", None, None)


def test_vazio_limpa_e_chave_antiga_sem_tipo_permanece():
    assert aplicar_chave_pix(None, "  ", "cpf", CPF) == (None, None)
    assert aplicar_chave_pix("", "chave-antiga", None, "chave-antiga") == (None, "chave-antiga")
    with pytest.raises(ValueError, match="Informe o tipo"):
        aplicar_chave_pix(None, "chave-nova", None, "chave-antiga")
    with pytest.raises(ValueError, match="Informe a chave"):
        aplicar_chave_pix("cpf", "", None, None)


def test_relatorio_imprime_tipo_e_chave_da_base():
    assert resolver_pix_receber(CPF, "cpf", "+5511987654321", "telefone") == (CPF, "cpf")
    assert resolver_pix_receber("  ", None, "+5511987654321", "telefone") == ("+5511987654321", "telefone")
    assert resolver_pix_receber(None, None, None, None) == (None, None)
    assert texto_pix_relatorio("cpf", CPF) == f"PIX CPF {CPF}"
    assert texto_pix_relatorio("telefone", "+5511987654321") == "PIX Celular +5511987654321"
    assert texto_pix_relatorio(None, "chave-antiga") == "PIX chave-antiga"
    assert texto_pix_relatorio("cpf", "  ") is None
