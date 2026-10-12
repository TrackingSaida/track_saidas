"""Regras puras de valor do fechamento de entregador/motoboy (sem banco)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, Mapping, Optional

_ZERO = Decimal("0.00")
_CHAVE_PRECO = {"shopee": "shopee_valor", "flex": "ml_valor", "avulso": "avulso_valor"}


def normalizar_servico(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "ml" in s or "mercado" in s:
        return "flex"
    return "avulso"


def saida_cancelada(status: Optional[str]) -> bool:
    return "cancel" in (status or "").strip().lower()


def preco_servico(precos: Mapping[str, Any], tipo: str) -> Decimal:
    try:
        return Decimal(str(precos.get(_CHAVE_PRECO.get(tipo, "avulso_valor")) or 0))
    except Exception:
        return _ZERO


def valor_saida(precos: Mapping[str, Any], servico: Optional[str], status: Optional[str],
                is_grande: bool, pacote_g_adicional: bool) -> Decimal:
    """Cancelado vale zero. Pacote G soma o preço do serviço de novo quando o adicional está ligado."""
    if saida_cancelada(status):
        return _ZERO
    preco = preco_servico(precos, normalizar_servico(servico))
    if pacote_g_adicional and is_grande:
        return preco * 2
    return preco


def valor_g_adicional(g_por_tipo: Mapping[str, int], precos: Mapping[str, Any], ativo: bool) -> Decimal:
    """Valor extra dos pacotes G não cancelados ({shopee, flex, avulso} -> quantidade)."""
    if not ativo:
        return _ZERO
    total = _ZERO
    for tipo, qtd in g_por_tipo.items():
        total += preco_servico(precos, tipo) * int(qtd or 0)
    return total.quantize(Decimal("0.01"))


def contar_g(saidas: Iterable[Any]) -> Dict[str, int]:
    """Conta pacotes G não cancelados por serviço."""
    out = {"shopee": 0, "ml": 0, "avulso": 0}
    for s in saidas:
        if not bool(getattr(s, "is_grande", False)) or saida_cancelada(getattr(s, "status", None)):
            continue
        tipo = normalizar_servico(getattr(s, "servico", None))
        out["ml" if tipo == "flex" else tipo] += 1
    out["total"] = out["shopee"] + out["ml"] + out["avulso"]
    return out


def ajustes_mudaram(atual: Mapping[str, Any], novo: Mapping[str, Any]) -> bool:
    """True quando algum valor/motivo do reajuste difere do gravado."""
    for k in ("valor_base", "valor_adicao", "valor_subtracao"):
        if k in novo and novo[k] is not None and Decimal(str(novo[k])) != Decimal(str(atual.get(k) or 0)):
            return True
    for k in ("motivo_adicao", "motivo_subtracao"):
        if k in novo and novo[k] is not None and ((novo[k] or "").strip() or None) != (atual.get(k) or None):
            return True
    return False
