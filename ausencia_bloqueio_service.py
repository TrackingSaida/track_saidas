"""Bloqueio de novas tentativas após limite de ausências."""
from __future__ import annotations

from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import SaidaHistorico

MAX_AUSENCIAS = 3
EVENTOS_AUSENCIA = ("ausente", "ausente_lote")
EVENTO_LIBERACAO = "liberacao_ausencias"


def contar_ausencias_em_eventos(eventos: list) -> int:
    count = 0
    for evento in eventos:
        ev = (evento or "").strip().lower()
        if ev in EVENTOS_AUSENCIA:
            count += 1
        elif ev == EVENTO_LIBERACAO:
            # Liberação reinicia a contagem efetiva do bloqueio (histórico permanece).
            count = 0
    return count


def contar_ausencias(db: Session, id_saida: int) -> int:
    rows = db.scalars(
        select(SaidaHistorico.evento)
        .where(SaidaHistorico.id_saida == id_saida)
        .order_by(SaidaHistorico.timestamp.asc(), SaidaHistorico.id.asc())
    ).all()
    return contar_ausencias_em_eventos(list(rows))


def esta_bloqueado_por_ausencias(db: Session, id_saida: int) -> Tuple[bool, int]:
    total = contar_ausencias(db, id_saida)
    return total >= MAX_AUSENCIAS, total


def raise_if_bloqueado_ausencias(db: Session, id_saida: int) -> int:
    bloqueado, total = esta_bloqueado_por_ausencias(db, id_saida)
    if bloqueado:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BLOQUEADO_AUSENCIAS",
                "ausencias": total,
                "message": (
                    f"Limite de {MAX_AUSENCIAS} tentativas de ausência atingido. "
                    "Somente a operação pode liberar uma nova tentativa."
                ),
            },
        )
    return total


def snapshot_bloqueio_ausencias(db: Session, id_saida: Optional[int]) -> dict:
    if not id_saida:
        return {"ausencias_total": 0, "bloqueado_ausencias": False}
    bloqueado, total = esta_bloqueado_por_ausencias(db, int(id_saida))
    return {"ausencias_total": total, "bloqueado_ausencias": bloqueado}
