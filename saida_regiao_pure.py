"""Região operacional = 3 primeiros dígitos do CEP de destino."""
from __future__ import annotations

import re
from typing import Optional


def digitos_cep(cep: Optional[str]) -> str:
    return re.sub(r"\D", "", cep or "")


def regiao_de_cep(cep: Optional[str]) -> Optional[str]:
    """Prefixo de 3 dígitos; None se CEP ausente ou com menos de 3 dígitos."""
    digits = digitos_cep(cep)
    if len(digits) < 3:
        return None
    return digits[:3]


def normalizar_filtro_regiao(valor: Optional[str]) -> Optional[str]:
    """Aceita só exatamente 3 dígitos; senão None (sem filtro)."""
    digits = digitos_cep(valor)
    if len(digits) != 3:
        return None
    return digits
