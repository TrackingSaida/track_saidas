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


def valor_entregas_agregadas(
    agregados: Iterable[Any],
    precos_por_motoboy: Mapping[int, Mapping[str, Any]],
    *,
    toggle_pacote_g: bool,
    regras_g: Mapping[int, Mapping[str, Any]],
    padrao_g: Optional[int],
) -> Dict[int, Decimal]:
    """Soma o valor das entregas a partir de contagens já agregadas (cancelado = 0)."""
    totais: Dict[int, Decimal] = {}
    for agg in agregados:
        if getattr(agg, "is_cancelado", False):
            continue
        mid = getattr(agg, "motoboy_id", None)
        if mid is None:
            continue
        mid = int(mid)
        tipo = getattr(agg, "tipo_servico", None) or "avulso"
        qtd = int(getattr(agg, "qtd", 0) or 0)
        unit = preco_servico(precos_por_motoboy.get(mid) or {}, tipo)
        total = totais.get(mid, _ZERO) + (unit * qtd)
        if toggle_pacote_g and getattr(agg, "is_grande", False):
            cat = getattr(agg, "categoria_g_id", None)
            cat_id = int(cat) if cat is not None else padrao_g
            total += adicional_g(unit, regras_g.get(cat_id) if cat_id is not None else None) * qtd
        totais[mid] = total
    return {mid: v.quantize(Decimal("0.01")) for mid, v in totais.items()}


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


CANAIS_APP = ("flex", "shopee")


def acerto_canal(qtd_app: Optional[int], qtd_sistema: int, preco: Any) -> Decimal:
    """(app − sistema) × preço. Sem quantidade no app, não há acerto."""
    if qtd_app is None:
        return _ZERO
    try:
        unit = Decimal(str(preco or 0))
    except Exception:
        unit = _ZERO
    return (Decimal(int(qtd_app) - int(qtd_sistema or 0)) * unit).quantize(Decimal("0.01"))


def snapshot_canal(canal: str, qtd_app: Optional[int], qtd_sistema: int, precos: Mapping[str, Any]) -> Dict[str, Any]:
    """Colunas gravadas de um canal. Sem quantidade no app, o canal fica vazio."""
    if qtd_app is None:
        return {f"qtd_app_{canal}": None, f"qtd_sistema_{canal}": None, f"preco_acerto_{canal}": None}
    return {
        f"qtd_app_{canal}": int(qtd_app),
        f"qtd_sistema_{canal}": int(qtd_sistema or 0),
        f"preco_acerto_{canal}": preco_servico(precos, canal).quantize(Decimal("0.01")),
    }


def valor_acerto_apps(colunas: Mapping[str, Any]) -> Decimal:
    total = _ZERO
    for canal in CANAIS_APP:
        total += acerto_canal(
            colunas.get(f"qtd_app_{canal}"),
            colunas.get(f"qtd_sistema_{canal}") or 0,
            colunas.get(f"preco_acerto_{canal}"),
        )
    return total.quantize(Decimal("0.01"))


def valor_final_fechamento(valor_base: Any, acerto_apps: Any, adicao: Any, subtracao: Any) -> Decimal:
    return (
        Decimal(str(valor_base or 0)) + Decimal(str(acerto_apps or 0))
        + Decimal(str(adicao or 0)) - Decimal(str(subtracao or 0))
    ).quantize(Decimal("0.01"))


def ajustes_mudaram(atual: Mapping[str, Any], novo: Mapping[str, Any]) -> bool:
    """True quando algum valor/motivo do reajuste difere do gravado.

    Quantidade do app só é comparada quando a chave veio; ali None significa limpar o canal.
    """
    for k in ("valor_base", "valor_adicao", "valor_subtracao"):
        if k in novo and novo[k] is not None and Decimal(str(novo[k])) != Decimal(str(atual.get(k) or 0)):
            return True
    for k in ("motivo_adicao", "motivo_subtracao"):
        if k in novo and novo[k] is not None and ((novo[k] or "").strip() or None) != (atual.get(k) or None):
            return True
    for canal in CANAIS_APP:
        k = f"qtd_app_{canal}"
        if k in novo and novo[k] != atual.get(k):
            return True
    return False
