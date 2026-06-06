"""Fuzzy matching para 'Você quis dizer?'."""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from address_normalizer import normalize_street_part, normalizeAddressQuery, normalize_cep

FUZZY_DID_YOU_MEAN_THRESHOLD = float(os.getenv("FUZZY_DID_YOU_MEAN_THRESHOLD", "0.72"))
FUZZY_LOW_SCORE_THRESHOLD = float(os.getenv("FUZZY_LOW_SCORE_THRESHOLD", "0.65"))


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


def extract_query_street(query: str, hints: Optional[dict] = None) -> str:
    """Extrai logradouro da query completa (remove número, CEP, cidade, UF)."""
    hints = hints or {}
    hint_rua = (hints.get("rua") or "").strip()
    if len(hint_rua) >= 3:
        return normalize_street_part(hint_rua)

    q = normalizeAddressQuery(query) or (query or "").strip()
    if not q:
        return ""

    # Primeiro segmento antes da vírgula = logradouro + número
    primary = q.split(",")[0].strip()
    primary = re.sub(r"\b\d{5}-?\d{3}\b", " ", primary)
    primary = re.sub(r"\b\d+[a-zA-Z]?\b", " ", primary)
    primary = re.sub(r"\s+", " ", primary).strip()
    street = normalize_street_part(primary)
    if len(street) >= 3:
        return street

    q = re.sub(r"\b\d{5}-?\d{3}\b", " ", q)
    q = re.sub(r"\b\d+[a-zA-Z]?\b", " ", q)
    for part in (hints.get("cidade"), hints.get("estado"), "Brasil"):
        if part and str(part).strip():
            q = re.sub(re.escape(str(part).strip()), " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\b[A-Za-z]{2}\b(?=\s*$)", " ", q)
    q = re.sub(r"\s*,\s*", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return normalize_street_part(q) or normalize_street_part(query)


def find_did_you_mean(
    query: str,
    candidates: List[Tuple[str, str, str]],
    threshold: Optional[float] = None,
    hints: Optional[dict] = None,
) -> Optional[Tuple[str, str, str, float]]:
    """Retorna (rua, cidade, estado, sim) se houver match fuzzy forte."""
    street_q = extract_query_street(query, hints)
    if len(street_q) < 3:
        street_q = normalize_street_part(query)
    if len(street_q) < 3:
        return None

    thresh = threshold if threshold is not None else FUZZY_DID_YOU_MEAN_THRESHOLD
    best: Optional[Tuple[str, str, str, float]] = None
    for rua, cidade, estado in candidates:
        sim = similarity(street_q, rua)
        if sim >= thresh and (best is None or sim > best[3]):
            best = (rua, cidade, estado, sim)
    return best
