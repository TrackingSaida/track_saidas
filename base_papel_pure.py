"""PRD-011: regras puras do papel do cliente (seller | base_parceira)."""
from __future__ import annotations

from typing import Optional

PAPEIS = frozenset({"seller", "base_parceira"})

# Clientes já cadastrados na subbase viram Base parceira; Sellers entram só no cadastro explícito.
PAPEL_PREENCHIDO_AO_PROMOVER = "base_parceira"


def papel_ao_promover_owner_para_base(*, qtd_owners: int, tipo_owner: Optional[str]) -> Optional[str]:
    """Papel a gravar em clientes sem papel na promoção. None = não preenche."""
    if int(qtd_owners or 0) != 1:
        return None
    if (tipo_owner or "").strip().lower() != "base":
        return None
    return PAPEL_PREENCHIDO_AO_PROMOVER


def normalizar_papel(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v not in PAPEIS:
        raise ValueError("papel deve ser seller ou base_parceira.")
    return v


def normalizar_papel_planilha(value: Optional[str]) -> str:
    """Planilha: seller | base (| base_parceira). Obrigatório; devolve enum interno."""
    if value is None or not str(value).strip():
        raise ValueError("Tipo obrigatório: seller ou base.")
    v = str(value).strip().lower()
    aliases = {
        "seller": "seller",
        "base": "base_parceira",
        "base_parceira": "base_parceira",
    }
    if v not in aliases:
        raise ValueError("Tipo inválido. Use seller ou base.")
    return aliases[v]


def exige_papel_obrigatorio(tipo_owner: Optional[str]) -> bool:
    return (tipo_owner or "").strip().lower() == "base"


def eh_seller(papel: Optional[str]) -> bool:
    return (papel or "").strip().lower() == "seller"


def eh_base_parceira(papel: Optional[str]) -> bool:
    return (papel or "").strip().lower() == "base_parceira"


def rotulo_tipo_planilha(papel: Optional[str]) -> Optional[str]:
    """Rótulo da planilha/UI a partir do papel interno."""
    if eh_base_parceira(papel):
        return "base"
    if eh_seller(papel):
        return "seller"
    return None


def permite_autenticacao_portal(papel: Optional[str], tipo_owner: Optional[str]) -> bool:
    """Owner subbase: todos os clientes. Owner base: só seller."""
    if not exige_papel_obrigatorio(tipo_owner):
        return True
    return eh_seller(papel)


def exige_cnpj_endereco_seller(*, tipo_owner: Optional[str], papel: Optional[str]) -> bool:
    """Seller (ou legado sem papel) em owner base exige CNPJ e endereço."""
    if not exige_papel_obrigatorio(tipo_owner):
        return False
    if papel is None:
        return True
    return eh_seller(papel)


def cnpj_endereco_completo(
    *,
    cnpj: Optional[str],
    rua: Optional[str],
    numero: Optional[str],
    bairro: Optional[str],
    cidade: Optional[str],
    cep: Optional[str],
) -> bool:
    return all(
        str(v or "").strip()
        for v in (cnpj, rua, numero, bairro, cidade, cep)
    )
