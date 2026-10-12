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


def adicional_g(preco: Decimal, regra: Optional[Mapping[str, Any]]) -> Decimal:
    """Adicional de um pacote G. Regra {modo: multiplicador|valor_fixo, valor}; sem regra = +1x o serviço."""
    if not regra:
        return preco
    try:
        valor = Decimal(str(regra.get("valor") or 0))
    except Exception:
        valor = _ZERO
    if regra.get("modo") == "valor_fixo":
        return valor.quantize(Decimal("0.01"))
    return (preco * valor).quantize(Decimal("0.01"))


def valor_saida(precos: Mapping[str, Any], servico: Optional[str], status: Optional[str],
                is_grande: bool, pacote_g_adicional: bool,
                regra_g: Optional[Mapping[str, Any]] = None) -> Decimal:
    """Cancelado vale zero. Pacote G soma o adicional da categoria quando o adicional está ligado."""
    if saida_cancelada(status):
        return _ZERO
    preco = preco_servico(precos, normalizar_servico(servico))
    if pacote_g_adicional and is_grande:
        return preco + adicional_g(preco, regra_g)
    return preco


def valor_g_adicional(g_por_tipo: Mapping[Any, int], precos: Mapping[str, Any], ativo: bool,
                      regras: Optional[Mapping[Any, Mapping[str, Any]]] = None) -> Decimal:
    """Valor extra dos pacotes G não cancelados.

    Chaves: tipo ({shopee, flex, avulso}) ou (categoria_id, tipo) quando há categorias.
    """
    if not ativo:
        return _ZERO
    total = _ZERO
    for chave, qtd in g_por_tipo.items():
        categoria, tipo = chave if isinstance(chave, tuple) else (None, chave)
        regra = (regras or {}).get(categoria) if categoria is not None else None
        total += adicional_g(preco_servico(precos, tipo), regra) * int(qtd or 0)
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
