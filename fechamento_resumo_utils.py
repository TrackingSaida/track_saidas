"""Agregação do resumo de fechamento (mesmo recorte do relatório)."""
from __future__ import annotations

import unicodedata
from decimal import Decimal
from typing import Any, Dict, List, Optional

_ZERO = Decimal("0.00")


def _norm_base(value: Optional[str]) -> str:
    return unicodedata.normalize("NFD", (value or "").strip().lower()).encode("ascii", "ignore").decode("ascii")


def base_filtro_ativo(base: Optional[str]) -> Optional[str]:
    """Retorna a base normalizada do filtro, ou None quando o filtro abrange todas."""
    alvo = _norm_base(base)
    if not alvo or alvo == "(todas)":
        return None
    return alvo


def base_prefiltro_sql_valores(base: Optional[str]) -> List[str]:
    """Valores para pré-filtro SQL (lower/trim), sem excluir variantes com/sem acento.

    A filtragem final continua em `saida_pertence_base` (acento, caixa e espaço).
    """
    raw = (base or "").strip()
    if not raw or raw.lower() == "(todas)":
        return []
    vals = {raw.lower(), _norm_base(raw)}
    return sorted(v for v in vals if v)


def saida_pertence_base(base_saida: Optional[str], base_filtro_norm: Optional[str]) -> bool:
    if base_filtro_norm is None:
        return True
    return _norm_base(base_saida) == base_filtro_norm


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return _ZERO


def _servico_resumo(
    feitos: int,
    cancelados: int,
    valor_feitos: Decimal,
    valor_cancelados: Decimal,
) -> Dict[str, Any]:
    return {
        "feitos": int(feitos),
        "cancelados": int(cancelados),
        "valor_feitos": valor_feitos.quantize(Decimal("0.01")),
        "valor_cancelados": valor_cancelados.quantize(Decimal("0.01")),
    }


def agregar_resumo_fechamento(
    itens: List[Dict[str, Any]],
    *,
    valor_adicao: Any = 0,
    valor_subtracao: Any = 0,
) -> Dict[str, Any]:
    """Consolida feitos, cancelados, G, valores por serviço e ajustes."""
    feitos_s = sum(int(r.get("shopee") or 0) for r in itens)
    feitos_f = sum(int(r.get("flex") or 0) for r in itens)
    feitos_a = sum(int(r.get("avulso") or 0) for r in itens)
    canc_s = sum(int(r.get("cancel_shopee") or 0) for r in itens)
    canc_f = sum(int(r.get("cancel_flex") or 0) for r in itens)
    canc_a = sum(int(r.get("cancel_avulso") or 0) for r in itens)
    val_s = sum((_dec(r.get("valor_shopee")) for r in itens), _ZERO)
    val_f = sum((_dec(r.get("valor_flex")) for r in itens), _ZERO)
    val_a = sum((_dec(r.get("valor_avulso")) for r in itens), _ZERO)
    val_cs = sum((_dec(r.get("valor_cancel_shopee")) for r in itens), _ZERO)
    val_cf = sum((_dec(r.get("valor_cancel_flex")) for r in itens), _ZERO)
    val_ca = sum((_dec(r.get("valor_cancel_avulso")) for r in itens), _ZERO)
    valor_bruto = sum((_dec(r.get("valor_feitos")) for r in itens), _ZERO)
    valor_cancelados = sum((_dec(r.get("valor_cancelados")) for r in itens), _ZERO)
    ajustes = (_dec(valor_adicao) - _dec(valor_subtracao)).quantize(Decimal("0.01"))
    return {
        "feitos": feitos_s + feitos_f + feitos_a,
        "cancelados": canc_s + canc_f + canc_a,
        "pacotes_grandes": sum(int(r.get("g_total") or 0) for r in itens),
        "valor_bruto": valor_bruto.quantize(Decimal("0.01")),
        "valor_cancelados": valor_cancelados.quantize(Decimal("0.01")),
        "ajustes": ajustes,
        "por_servico": {
            "shopee": _servico_resumo(feitos_s, canc_s, val_s, val_cs),
            "flex": _servico_resumo(feitos_f, canc_f, val_f, val_cf),
            "avulso": _servico_resumo(feitos_a, canc_a, val_a, val_ca),
        },
    }
