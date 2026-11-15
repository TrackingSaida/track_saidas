"""PRD-011 Fatia 2: regras puras do repasse a pagar para base parceira (sem banco).

Cálculo irmão de `fechamento_valor_pure.py`: o preço a pagar da base herda o global de
Preços de Entrega e só o canal diferente do global vira exceção.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from base_papel_pure import eh_base_parceira
from fechamento_valor_pure import normalizar_servico, saida_cancelada, valor_final_fechamento

CENTAVO = Decimal("0.01")
ZERO = Decimal("0.00")

# Canais do preço a pagar (mesmas chaves de entregador_preco_global).
CANAIS_PRECO = ("shopee", "ml", "avulso")
CAMPOS_PRECO = {"shopee": "shopee_valor", "ml": "ml_valor", "avulso": "avulso_valor"}

ORIGEM_PADRAO = "padrao"
ORIGEM_PROPRIO = "proprio"

# Saídas que contam no repasse enquanto repasse_base_id estiver preenchido.
# Cancelado entra só como contagem (valor zero), igual ao fechamento de motoboy.
STATUS_REPASSE_VALIDOS = (
    "enviado_para_base",
    "saiu",
    "saiu pra entrega",
    "saiu_pra_entrega",
    "saiu_para_entrega",
    "em_rota",
    "entregue",
    "ausente",
)
STATUS_REPASSE_CANCELADOS = ("cancelado", "cancelada")
STATUS_REPASSE_CONSULTA = STATUS_REPASSE_VALIDOS + STATUS_REPASSE_CANCELADOS

STATUS_GERADO = "GERADO"
STATUS_REAJUSTADO = "REAJUSTADO"
STATUS_PAGO = "PAGO"
STATUS_ELEGIVEIS_PAGAMENTO = (STATUS_GERADO, STATUS_REAJUSTADO)


def quantizar(valor: Any) -> Decimal:
    try:
        return Decimal(str(valor if valor is not None else 0)).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ZERO


def validar_destino_repasse(
    *, papel: Optional[str], entrega_pedidos: bool, ativo: bool
) -> Optional[str]:
    """Mensagem de erro quando a base não pode receber saída; None quando pode."""
    if not eh_base_parceira(papel):
        return "Só uma Base parceira pode receber pedidos para entregar."
    if not entrega_pedidos:
        return "Esta base não está marcada para entregar pedidos."
    if not ativo:
        return "Esta base está inativa."
    return None


def normalizar_preco_pagar(valor: Any) -> Optional[Decimal]:
    """Vazio vira None. Valor com mais de 2 casas é arredondado; negativo é rejeitado."""
    if valor is None:
        return None
    if isinstance(valor, str) and not valor.strip():
        return None
    try:
        dec = Decimal(str(valor).strip().replace(",", ".") if isinstance(valor, str) else str(valor))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Valor inválido para preço a pagar.") from exc
    if not dec.is_finite():
        raise ValueError("Valor inválido para preço a pagar.")
    if dec < 0:
        raise ValueError("Preço a pagar não pode ser negativo.")
    return dec.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def excecoes_preco_pagar(
    entrada: Mapping[str, Any], global_precos: Mapping[str, Any]
) -> Dict[str, Optional[Decimal]]:
    """Exceção por canal. Vazio ou igual ao global não grava (acompanha o global depois).

    `entrada` e `global_precos` usam as chaves shopee_valor, ml_valor, avulso_valor.
    """
    out: Dict[str, Optional[Decimal]] = {}
    for canal in CANAIS_PRECO:
        campo = CAMPOS_PRECO[canal]
        novo = normalizar_preco_pagar(entrada.get(campo))
        padrao = quantizar(global_precos.get(campo))
        out[campo] = None if novo is None or novo == padrao else novo
    return out


def resolver_precos_pagar(
    global_precos: Mapping[str, Any], excecao: Optional[Mapping[str, Any]]
) -> Dict[str, Decimal]:
    """Preço efetivo por canal: exceção da base quando existir, senão o global."""
    out: Dict[str, Decimal] = {}
    for canal in CANAIS_PRECO:
        campo = CAMPOS_PRECO[canal]
        proprio = (excecao or {}).get(campo)
        out[campo] = quantizar(proprio) if proprio is not None else quantizar(global_precos.get(campo))
    return out


def origem_precos_pagar(excecao: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """Rótulo por campo: Padrão ou Preço próprio."""
    return {
        CAMPOS_PRECO[canal]: (
            ORIGEM_PROPRIO if (excecao or {}).get(CAMPOS_PRECO[canal]) is not None else ORIGEM_PADRAO
        )
        for canal in CANAIS_PRECO
    }


def canal_preco(servico: Optional[str]) -> str:
    """shopee | ml | avulso (Flex vira ml)."""
    tipo = normalizar_servico(servico)
    return "ml" if tipo == "flex" else tipo


def calcular_repasse(
    saidas: Iterable[Tuple[date, Optional[str], Optional[str]]],
    precos: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Decimal]:
    """Agrega por dia operacional. `saidas`: (dia, servico, status).

    Cancelado conta em `cancelados` e vale zero. Retorna (itens por dia, valor_base).
    """
    efetivos = {CAMPOS_PRECO[c]: quantizar(precos.get(CAMPOS_PRECO[c])) for c in CANAIS_PRECO}
    por_dia: Dict[date, Dict[str, Any]] = {}
    for dia, servico, status in saidas:
        item = por_dia.setdefault(
            dia, {"data": dia, "shopee": 0, "ml": 0, "avulso": 0, "cancelados": 0, "valor_dia": ZERO}
        )
        if saida_cancelada(status):
            item["cancelados"] += 1
            continue
        canal = canal_preco(servico)
        item[canal] += 1
        item["valor_dia"] += efetivos[CAMPOS_PRECO[canal]]
    itens = [por_dia[d] for d in sorted(por_dia)]
    for item in itens:
        item["valor_dia"] = quantizar(item["valor_dia"])
    total = quantizar(sum((i["valor_dia"] for i in itens), ZERO))
    return itens, total


def totais_canais(itens: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    return {
        "shopee": sum(int(i.get("shopee") or 0) for i in itens),
        "ml": sum(int(i.get("ml") or 0) for i in itens),
        "avulso": sum(int(i.get("avulso") or 0) for i in itens),
        "cancelados": sum(int(i.get("cancelados") or 0) for i in itens),
    }


def valor_final_repasse(valor_base: Any, adicao: Any, subtracao: Any) -> Decimal:
    """Mesma conta do fechamento de motoboy, sem acerto de apps."""
    return valor_final_fechamento(valor_base, 0, adicao, subtracao)


def periodos_sobrepostos(
    existentes: Iterable[Tuple[date, date]], inicio: date, fim: date
) -> Optional[Tuple[date, date]]:
    """Primeiro período existente que cobre parte de [inicio, fim]."""
    for ini_e, fim_e in sorted(existentes):
        if ini_e <= fim and fim_e >= inicio:
            return ini_e, fim_e
    return None


def normalizar_status_repasse(status: Optional[str]) -> str:
    return (status or "").strip().upper()


def status_permite_reajuste(status: Optional[str]) -> bool:
    return normalizar_status_repasse(status) in (STATUS_GERADO, STATUS_REAJUSTADO)


def status_elegivel_pagamento(status: Optional[str]) -> bool:
    return normalizar_status_repasse(status) in STATUS_ELEGIVEIS_PAGAMENTO
