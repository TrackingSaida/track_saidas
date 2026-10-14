"""Regra de valor do extrato financeiro do motoboy (Minhas entregas)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional, Tuple

from entregador_fechamento_status_pure import STATUS_PAGO, normalizar_status_fechamento

MODO_GRUPO_ENTREGUE = "grupo_entregue"
MODO_TODOS = "todos"
MODO_CANCELADOS = "cancelados"

SITUACAO_SEM_FECHAMENTO = "sem_fechamento"
SITUACAO_GERADO = "gerado"
SITUACAO_PAGO = "pago"


def valor_extrato_por_filtro(
    valor_feitos: Decimal,
    valor_cancelados: Decimal,
    modo: str,
) -> Decimal:
    """Aplica a regra de total conforme o filtro de status.

    - todos: entregues + cancelados
    - grupo_entregue: só entregues (cancelados já estão fora; sem multa)
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
        # Cancelados já não entram em valor_feitos; não descontar de novo.
        total = feitos
    return total.quantize(Decimal("0.01"))


def situacao_fechamento_periodo(
    fechamentos: Iterable[Tuple[date, date, Optional[str]]],
    periodo_inicio: date,
    periodo_fim: date,
) -> str:
    """Situação dos fechamentos que tocam o período consultado.

    - sem_fechamento: nenhum fechamento cruza o período
    - pago: todos os dias do período estão em fechamentos PAGO
    - gerado: existe fechamento, mas parte do período ainda não foi paga
    """
    dias_pagos: set[date] = set()
    algum = False
    for inicio, fim, status in fechamentos:
        if fim < periodo_inicio or inicio > periodo_fim:
            continue
        algum = True
        if normalizar_status_fechamento(status) != STATUS_PAGO:
            continue
        d = max(inicio, periodo_inicio)
        ate = min(fim, periodo_fim)
        while d <= ate:
            dias_pagos.add(d)
            d += timedelta(days=1)
    if not algum:
        return SITUACAO_SEM_FECHAMENTO
    total_dias = (periodo_fim - periodo_inicio).days + 1
    if len(dias_pagos) >= total_dias:
        return SITUACAO_PAGO
    return SITUACAO_GERADO
