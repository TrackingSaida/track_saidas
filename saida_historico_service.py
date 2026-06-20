"""Consulta compartilhada de histórico de saída (saida_historico)."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import SaidaHistorico, User
from saida_operacional_utils import rotulo_acao_evento


class SaidaHistoricoItemOut(BaseModel):
    """Item completo do histórico (endpoint operacional/admin)."""

    id: int
    id_saida: int
    evento: str
    timestamp: datetime
    status_anterior: Optional[str] = None
    status_novo: Optional[str] = None
    user_id: Optional[int] = None
    usuario_nome: Optional[str] = None
    motoboy_id_anterior: Optional[int] = None
    motoboy_id_novo: Optional[int] = None
    acao_label: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class EntregaHistoricoItemOut(BaseModel):
    """Subset do histórico para timeline mobile."""

    id: int
    evento: str
    timestamp: datetime
    usuario_nome: Optional[str] = None
    acao_label: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


def listar_historico_saida(db: Session, id_saida: int) -> List[SaidaHistoricoItemOut]:
    """Lista eventos da saída ordenados por timestamp (asc)."""
    rows = db.execute(
        select(SaidaHistorico, User.username)
        .outerjoin(User, SaidaHistorico.user_id == User.id)
        .where(SaidaHistorico.id_saida == id_saida)
        .order_by(SaidaHistorico.timestamp.asc())
    ).all()
    out: List[SaidaHistoricoItemOut] = []
    for row in rows:
        h, username = row[0], row[1]
        evento_norm = (h.evento or "").strip().lower()
        out.append(
            SaidaHistoricoItemOut(
                id=h.id,
                id_saida=h.id_saida,
                evento=h.evento,
                timestamp=h.timestamp,
                status_anterior=h.status_anterior,
                status_novo=h.status_novo,
                user_id=h.user_id,
                usuario_nome=username,
                motoboy_id_anterior=h.motoboy_id_anterior,
                motoboy_id_novo=h.motoboy_id_novo,
                acao_label=rotulo_acao_evento(evento_norm),
            )
        )
    return out


def projetar_historico_mobile(items: List[SaidaHistoricoItemOut]) -> List[EntregaHistoricoItemOut]:
    """Projeta histórico completo para payload enxuto da timeline mobile."""
    return [
        EntregaHistoricoItemOut(
            id=item.id,
            evento=item.evento,
            timestamp=item.timestamp,
            usuario_nome=item.usuario_nome,
            acao_label=item.acao_label,
        )
        for item in items
    ]
