"""Helpers para pacotes ainda na base (entrada NA_BASE e, opcionalmente, coletado)."""
from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from entrada_na_base_pure import (
    classify_servico_na_base,
    contar_ainda_na_base_por_marketplace,
    contar_cancelados_apos_entrada_por_marketplace,
)
from models import Saida

STATUS_NA_BASE = "NA_BASE"
STATUS_COLETADO = "coletado"

__all__ = [
    "STATUS_NA_BASE",
    "STATUS_COLETADO",
    "listar_ainda_na_base",
    "contar_ainda_na_base",
    "detalhe_ainda_na_base_por_dia",
    "classify_servico_na_base",
    "contar_ainda_na_base_por_marketplace",
    "listar_cancelados_apos_entrada",
    "contar_cancelados_apos_entrada_por_marketplace",
]


def _conds_status_cancelado():
    return func.lower(Saida.status).in_(("cancelado", "cancelada"))


def _conds_status_na_base():
    return or_(
        Saida.status == STATUS_NA_BASE,
        func.lower(Saida.status) == "na_base",
        func.lower(Saida.status) == "na base",
    )


def _conds_status_coletado():
    return func.lower(Saida.status) == STATUS_COLETADO


def _conds_status_ainda_na_base(*, incluir_coletado: bool):
    """Estoque físico na base: NA_BASE; com incluir_coletado=True também status coletado."""
    if incluir_coletado:
        return or_(_conds_status_na_base(), _conds_status_coletado())
    return _conds_status_na_base()


def listar_ainda_na_base(
    db: Session,
    sub_base: str,
    data_inicio: date,
    data_fim: date,
    *,
    incluir_coletado: bool = False,
) -> List[Saida]:
    """Saídas ainda na base da sub_base com Saida.data no intervalo [data_inicio, data_fim].

    Por padrão conta só NA_BASE (entrada sem saída — usado pelo alerta push).
    Com incluir_coletado=True também inclui status coletado (estoque unificado do dashboard).
    """
    return list(
        db.scalars(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.codigo.isnot(None),
                Saida.data >= data_inicio,
                Saida.data <= data_fim,
                _conds_status_ainda_na_base(incluir_coletado=incluir_coletado),
            )
        ).all()
    )


def listar_cancelados_apos_entrada(
    db: Session,
    sub_base: str,
    ids_entrada_periodo: Iterable[int],
) -> List[Saida]:
    """Pacotes com entrada no período e status cancelado (explica parte do gap entradas−saídas)."""
    ids = [int(i) for i in ids_entrada_periodo]
    if not ids:
        return []
    return list(
        db.scalars(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.id_saida.in_(ids),
                Saida.codigo.isnot(None),
                _conds_status_cancelado(),
            )
        ).all()
    )


def contar_ainda_na_base(
    db: Session,
    sub_base: str,
    data_inicio: date,
    data_fim: date,
    *,
    incluir_coletado: bool = False,
) -> int:
    return len(
        listar_ainda_na_base(
            db,
            sub_base,
            data_inicio,
            data_fim,
            incluir_coletado=incluir_coletado,
        )
    )


def detalhe_ainda_na_base_por_dia(
    rows: List[Saida],
) -> List[Tuple[str, int]]:
    """Retorna [(YYYY-MM-DD, qty), ...] ordenado do mais recente para o mais antigo."""
    na_base_por_dia: Dict[str, int] = {}
    for s in rows:
        dia = (
            s.data.isoformat()
            if s.data
            else (s.timestamp.date().isoformat() if s.timestamp else None)
        )
        if not dia:
            continue
        na_base_por_dia[dia] = na_base_por_dia.get(dia, 0) + 1
    return sorted(na_base_por_dia.items(), key=lambda x: x[0], reverse=True)
