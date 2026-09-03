"""Lógica pura de classificação/contagem de NA_BASE por marketplace."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Set


def classify_servico_na_base(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "mercado" in s or "ml" in s or "flex" in s:
        return "mercado_livre"
    return "avulso"


def contar_ainda_na_base_por_marketplace(rows: Iterable[Any]) -> Dict[str, int]:
    """Conta NA_BASE por serviço (mesmas regras de classificação do dashboard)."""
    out = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
    for row in rows:
        out[classify_servico_na_base(getattr(row, "servico", None))] += 1
    return out


def contar_cancelados_apos_entrada_por_marketplace(
    primeira_entrada: Mapping[int, tuple],
    cancelados_ids: Set[int],
) -> Dict[str, int]:
    """Conta cancelados após entrada no período, pelo serviço da 1ª entrada."""
    out = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
    for sid in cancelados_ids:
        servico, _ = primeira_entrada.get(int(sid), (None, None))
        out[classify_servico_na_base(servico)] += 1
    return out
