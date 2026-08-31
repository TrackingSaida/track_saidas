"""Regra de valor do extrato financeiro do motoboy (Minhas entregas)."""
from __future__ import annotations

from decimal import Decimal

MODO_GRUPO_ENTREGUE = "grupo_entregue"
MODO_TODOS = "todos"
MODO_CANCELADOS = "cancelados"


def valor_extrato_por_filtro(
    valor_feitos: Decimal,
    valor_cancelados: Decimal,
    modo: str,
) -> Decimal:
    """Aplica a regra de total conforme o filtro de status.

    - todos: entregues + cancelados
    - grupo_entregue: entregues - cancelados
    - cancelados: só cancelados
    """
    feitos = Decimal(valor_feitos or 0)
    cancelados = Decimal(valor_cancelados or 0)
    key = (modo or MODO_GRUPO_ENTREGUE).strip().lower()
    if key == MODO_TODOS:
        total = feitos + cancelados
    elif key == MODO_CANCELADOS:
        total = cancelados
    else:
        total = feitos - cancelados
    return total.quantize(Decimal("0.01"))
