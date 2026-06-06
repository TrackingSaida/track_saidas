"""Normalização de endereços e queries para busca inteligente."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


def normalize_cep(cep: Optional[str]) -> str:
    digits = re.sub(r"\D", "", cep or "")
    return digits[:8] if len(digits) >= 8 else digits


def normalize_address_text(text: Optional[str]) -> str:
    raw = (text or "").strip().lower()
    raw = unicodedata.normalize("NFD", raw)
    raw = "".join(c for c in raw if unicodedata.category(c) != "Mn")
    raw = re.sub(r"\bn[º°o]\b", "numero", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()

_ABBREV_PREFIX = {
    r"^r\.?\s+": "Rua ",
    r"^rua\s+": "Rua ",
    r"^av\.?\s+": "Avenida ",
    r"^avenida\s+": "Avenida ",
    r"^al\.?\s+": "Alameda ",
    r"^alameda\s+": "Alameda ",
    r"^rod\.?\s+": "Rodovia ",
    r"^rodovia\s+": "Rodovia ",
    r"^tv\.?\s+": "Travessa ",
    r"^trav\.?\s+": "Travessa ",
    r"^travessa\s+": "Travessa ",
    r"^est\.?\s+": "Estrada ",
    r"^estrada\s+": "Estrada ",
    r"^pc\.?\s+": "Praça ",
    r"^praca\s+": "Praça ",
    r"^praça\s+": "Praça ",
}


def _strip_accents(text: str) -> str:
    raw = unicodedata.normalize("NFD", text)
    return "".join(c for c in raw if unicodedata.category(c) != "Mn")


def normalize_street_part(text: Optional[str]) -> str:
    return normalize_address_text(text or "")


def normalize_numero_part(numero: Optional[str]) -> str:
    raw = re.sub(r"\D", "", numero or "")
    return raw or normalize_address_text(numero or "")


def normalize_address_key(
    rua: Optional[str],
    numero: Optional[str],
    cep: Optional[str] = None,
) -> str:
    rua_n = normalize_street_part(rua)
    num_n = normalize_numero_part(numero)
    cep_n = normalize_cep(cep)
    return f"{rua_n}|{num_n}|{cep_n}"


_ESTADO_NOME_TO_UF = {
    "acre": "AC",
    "alagoas": "AL",
    "amapa": "AP",
    "amazonas": "AM",
    "bahia": "BA",
    "ceara": "CE",
    "distrito federal": "DF",
    "espirito santo": "ES",
    "goias": "GO",
    "maranhao": "MA",
    "mato grosso": "MT",
    "mato grosso do sul": "MS",
    "minas gerais": "MG",
    "para": "PA",
    "paraiba": "PB",
    "parana": "PR",
    "pernambuco": "PE",
    "piaui": "PI",
    "rio de janeiro": "RJ",
    "rio grande do norte": "RN",
    "rio grande do sul": "RS",
    "rondonia": "RO",
    "roraima": "RR",
    "santa catarina": "SC",
    "sao paulo": "SP",
    "sergipe": "SE",
    "tocantins": "TO",
}


def normalize_estado_uf(estado: Optional[str], iso3166: Optional[str] = None) -> str:
    """Converte nome completo ou ISO BR-XX para sigla UF (evita [:2] em 'São Paulo' → 'SÃ')."""
    if iso3166:
        iso = str(iso3166).strip().upper()
        if iso.startswith("BR-") and len(iso) >= 5:
            return iso[3:5]
        if len(iso) == 2 and iso.isalpha():
            return iso
    raw = (estado or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw).upper()
    if len(compact) == 2 and compact.isascii() and compact.isalpha():
        return compact
    key = _strip_accents(raw).lower()
    return _ESTADO_NOME_TO_UF.get(key, "")


def normalizeAddressQuery(query: str) -> str:
    """Expande abreviações e aplica title case para busca."""
    q = (query or "").strip()
    if not q:
        return ""
    lower = q.lower()
    for pattern, replacement in _ABBREV_PREFIX.items():
        if re.match(pattern, lower, re.IGNORECASE):
            rest = re.sub(pattern, "", lower, count=1, flags=re.IGNORECASE).strip()
            title_rest = " ".join(w.capitalize() for w in rest.split())
            return (replacement.strip() + " " + title_rest).strip()
    return " ".join(w.capitalize() for w in q.split())
