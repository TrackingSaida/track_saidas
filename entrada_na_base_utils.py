"""Helpers para pacotes com entrada na base ainda sem saída (status NA_BASE)."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import Saida

STATUS_NA_BASE = "NA_BASE"


def _conds_status_na_base():
    return or_(
        Saida.status == STATUS_NA_BASE,
        func.lower(Saida.status) == "na_base",
        func.lower(Saida.status) == "na base",
    )


def listar_ainda_na_base(
    db: Session,
    sub_base: str,
    data_inicio: date,
    data_fim: date,
) -> List[Saida]:
    """Saídas NA_BASE da sub_base com Saida.data no intervalo [data_inicio, data_fim]."""
    return list(
        db.scalars(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.codigo.isnot(None),
                Saida.data >= data_inicio,
                Saida.data <= data_fim,
                _conds_status_na_base(),
            )
        ).all()
    )


def contar_ainda_na_base(
    db: Session,
    sub_base: str,
    data_inicio: date,
    data_fim: date,
) -> int:
    return len(listar_ainda_na_base(db, sub_base, data_inicio, data_fim))


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
