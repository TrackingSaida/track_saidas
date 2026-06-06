"""Fuzzy matching para 'Você quis dizer?'."""
from __future__ import annotations

from typing import List, Optional, Tuple

from address_normalizer import normalize_street_part


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def similarity(a: str, b: str) -> float:
    na = normalize_street_part(a)
    nb = normalize_street_part(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    dist = _levenshtein(na, nb)
    max_len = max(len(na), len(nb))
    return 1.0 - (dist / max_len)


def find_did_you_mean(
    query: str,
    candidates: List[Tuple[str, str, str]],
    threshold: float = 0.82,
) -> Optional[Tuple[str, str, str, float]]:
    """Retorna (rua, cidade, estado, sim) se houver match fuzzy forte."""
    best: Optional[Tuple[str, str, str, float]] = None
    for rua, cidade, estado in candidates:
        sim = similarity(query, rua)
        if sim >= threshold and (best is None or sim > best[3]):
            best = (rua, cidade, estado, sim)
    return best
