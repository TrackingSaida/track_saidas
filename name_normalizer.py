"""Normalização de nomes de pessoas (padrão do sistema).

Regra de display:
- cada palavra com inicial maiúscula e restante minúsculo;
- partículas pt-BR (de/da/do/das/dos/e) ficam em minúsculo quando não forem a 1ª palavra;
- espaços colapsados; trim.
"""
from __future__ import annotations

from typing import Optional

_MINOR_WORDS = frozenset({"de", "da", "do", "das", "dos", "e"})


def normalize_person_name(value: Optional[str]) -> Optional[str]:
    """Trim, colapsa espaços e aplica o padrão de nome do sistema."""
    s = " ".join((value or "").split())
    if not s:
        return None
    parts = s.split(" ")
    out = []
    for i, word in enumerate(parts):
        lower = word.casefold()
        if i > 0 and lower in _MINOR_WORDS:
            out.append(lower)
            continue
        # Preserva hífen interno (ex.: Ana-Clara → Ana-Clara)
        chunks = lower.split("-")
        titled = "-".join(
            (c[:1].upper() + c[1:]) if c else c
            for c in chunks
        )
        out.append(titled)
    return " ".join(out)


def normalize_display_name(value: Optional[str], *, fallback: str = "") -> str:
    """Normaliza string já montada para exibição (nunca retorna None)."""
    return normalize_person_name(value) or (fallback or "")


def format_person_full_name(
    nome: Optional[str] = None,
    sobrenome: Optional[str] = None,
    *,
    username: Optional[str] = None,
    fallback: str = "",
) -> str:
    """Monta nome completo normalizado a partir de partes."""
    raw = " ".join(
        p for p in [
            (nome or "").strip(),
            (sobrenome or "").strip(),
        ] if p
    ).strip()
    if raw:
        return normalize_display_name(raw)
    if username and str(username).strip():
        return normalize_display_name(str(username).strip()) or str(username).strip()
    return fallback or ""


def person_name_sort_key(value: Optional[str]) -> str:
    """Chave de ordenação alfabética pt-BR (casefold)."""
    return (value or "").strip().casefold()
