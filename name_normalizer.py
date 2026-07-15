"""Normalização de nomes de pessoas (Title Case)."""
from __future__ import annotations

from typing import Optional


def normalize_person_name(value: Optional[str]) -> Optional[str]:
    """Trim, colapsa espaços e aplica capitalize por palavra."""
    s = " ".join((value or "").split())
    if not s:
        return None
    return " ".join(w.capitalize() for w in s.split())
