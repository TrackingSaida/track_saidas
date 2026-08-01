"""Serviço de Conferência de Saída (motoboy + dia operacional)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import ConferenciaSaida, Motoboy, Owner, Saida, SaidaHistorico, User

STATUS_PENDENTE = "pendente"
STATUS_RECONFERIR = "reconferir"
STATUS_CONFERIDA = "conferida"

LABEL_FECHAMENTO = {
    STATUS_CONFERIDA: "Conferido",
    STATUS_PENDENTE: "Não conferido",
    STATUS_RECONFERIR: "Não conferido",
}


def owner_conferencia_habilitada(db: Session, sub_base: str, user: Optional[User] = None) -> bool:
    if user is not None and bool(getattr(user, "conferencia_saida_habilitada", False)):
        return True
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return bool(owner and getattr(owner, "conferencia_saida_habilitada", False))


def _owner_id_for_sub_base(db: Session, sub_base: str) -> Optional[int]:
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return int(owner.id_owner) if owner else None


def upsert_conferencia_apos_iniciar_rota(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    qtd: int,
) -> Optional[ConferenciaSaida]:
    if qtd <= 0:
        return None
    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sub_base == sub_base,
            ConferenciaSaida.motoboy_id == motoboy_id,
            ConferenciaSaida.data_ref == data_ref,
        )
    )
    now = datetime.utcnow()
    if row is None:
        row = ConferenciaSaida(
            sub_base=sub_base,
            owner_id=_owner_id_for_sub_base(db, sub_base),
            motoboy_id=motoboy_id,
            data_ref=data_ref,
            status=STATUS_PENDENTE,
            ultima_abertura_em=now,
            qtd_no_momento=qtd,
        )
        db.add(row)
        return row

    row.qtd_no_momento = qtd
    row.ultima_abertura_em = now
    if row.status == STATUS_CONFERIDA:
        row.status = STATUS_RECONFERIR
        row.conferido_por = None
        row.conferido_em = None
    return row


def _servico_bucket(servico: Optional[str]) -> str:
    s = (servico or "").strip().lower()
    if s in ("shopee",):
        return "shopee"
    if s in ("ml", "mercado_livre", "mercado livre", "mercadolivre"):
        return "ml"
    return "avulso"


def somar_servicos_saidas(rows: List[Saida]) -> Tuple[int, int, int]:
    shopee = ml = avulso = 0
    for s in rows:
        b = _servico_bucket(s.servico)
        if b == "shopee":
            shopee += 1
        elif b == "ml":
            ml += 1
        else:
            avulso += 1
    return shopee, ml, avulso


def listar_saidas_motoboy_dia(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
) -> List[Saida]:
    return list(
        db.scalars(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.motoboy_id == motoboy_id,
                Saida.codigo.isnot(None),
                Saida.data == data_ref,
            )
        ).all()
    )


def carregar_nomes_motoboy(db: Session, motoboy_ids: List[int]) -> Dict[int, str]:
    if not motoboy_ids:
        return {}
    # Motoboy.user_id → User.username; fallback nome do relacionamento
    from models import User as UserModel

    rows = db.execute(
        select(Motoboy.id_motoboy, UserModel.username, UserModel.nome, UserModel.sobrenome).outerjoin(
            UserModel, UserModel.id == Motoboy.user_id
        ).where(Motoboy.id_motoboy.in_(motoboy_ids))
    ).all()
    out: Dict[int, str] = {}
    for mid, username, nome, sobrenome in rows:
        full = " ".join(p for p in [(nome or "").strip(), (sobrenome or "").strip()] if p).strip()
        out[int(mid)] = full or (username or "").strip() or f"Motoboy {mid}"
    return out


def label_fechamento(status: str) -> str:
    return LABEL_FECHAMENTO.get(status, "Não conferido")


def conferencia_por_dia_periodo(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    periodo_inicio: date,
    periodo_fim: date,
) -> List[dict]:
    rows = list(
        db.scalars(
            select(ConferenciaSaida).where(
                ConferenciaSaida.sub_base == sub_base,
                ConferenciaSaida.motoboy_id == motoboy_id,
                ConferenciaSaida.data_ref >= periodo_inicio,
                ConferenciaSaida.data_ref <= periodo_fim,
            ).order_by(ConferenciaSaida.data_ref.asc())
        ).all()
    )
    return [
        {
            "data": r.data_ref.isoformat(),
            "status": r.status,
            "label": label_fechamento(r.status),
        }
        for r in rows
    ]


def conferir_saida(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    user_id: Optional[int],
) -> ConferenciaSaida:
    row = db.scalar(
        select(ConferenciaSaida).where(
            ConferenciaSaida.sub_base == sub_base,
            ConferenciaSaida.motoboy_id == motoboy_id,
            ConferenciaSaida.data_ref == data_ref,
        )
    )
    if row is None:
        raise ValueError("CONFERENCIA_NAO_ENCONTRADA")

    saidas = listar_saidas_motoboy_dia(
        db, sub_base=sub_base, motoboy_id=motoboy_id, data_ref=data_ref
    )
    now = datetime.utcnow()
    row.status = STATUS_CONFERIDA
    row.conferido_por = user_id
    row.conferido_em = now
    row.qtd_no_momento = len(saidas)

    for s in saidas:
        db.add(
            SaidaHistorico(
                id_saida=s.id_saida,
                evento="saida_conferida",
                status_anterior=s.status,
                status_novo=s.status,
                user_id=user_id,
            )
        )
    return row
