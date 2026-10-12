"""Validadores de documentos e formatos brasileiros (CPF, CNPJ, celular, CEP, UF)."""
from __future__ import annotations

import re
from typing import Optional

from codigo_normalizer import _is_telefone_brasil

UFS = frozenset(
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
)


def only_digits(value: Optional[str]) -> str:
    return re.sub(r"\D+", "", value or "")


def is_cpf_valido(value: Optional[str]) -> bool:
    d = only_digits(value)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d[i]) * (size + 1 - i) for i in range(size))
        dv = (total * 10) % 11 % 10
        if dv != int(d[size]):
            return False
    return True


def is_cnpj_valido(value: Optional[str]) -> bool:
    d = only_digits(value)
    if len(d) != 14 or d == d[0] * 14:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    for pesos, pos in ((pesos1, 12), (pesos2, 13)):
        total = sum(int(d[i]) * pesos[i] for i in range(len(pesos)))
        resto = total % 11
        dv = 0 if resto < 2 else 11 - resto
        if dv != int(d[pos]):
            return False
    return True


def normalizar_celular(value: Optional[str]) -> Optional[str]:
    """Celular BR com DDD: 11 dígitos e terceiro dígito 9. Retorna só dígitos ou None."""
    digits = _is_telefone_brasil(value or "")
    if not digits or len(digits) != 11 or digits[2] != "9":
        return None
    return digits


def is_cep_valido(value: Optional[str]) -> bool:
    return len(only_digits(value)) == 8


def is_uf_valida(value: Optional[str]) -> bool:
    return (value or "").strip().upper() in UFS
