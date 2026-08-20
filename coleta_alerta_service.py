from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from base_fechamento_routes import _resumo_calendario_fechamento
from models import BasePreco, Owner
from push_notification_service import send_to_staff_sub_base


def notificar_coletas_pendentes(db: Session, *, forcar: bool = False) -> dict:
    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    if not forcar and agora.hour not in (19, 20, 21, 22, 23):
        return {"ignorado": True, "motivo": "fora_da_janela", "hora": agora.hour}

    hoje = agora.date()
    owners = db.scalars(
        select(Owner).where(
            Owner.ativo.is_(True),
            Owner.ignorar_coleta.is_(False),
        )
    ).all()
    notificacoes = bases_pendentes = 0
    por_sub_base = {}
    for owner in owners:
        sub_base = (owner.sub_base or "").strip()
        if not sub_base:
            continue
        bases = db.scalars(
            select(BasePreco).where(
                BasePreco.sub_base == sub_base,
                BasePreco.ativo.is_(True),
                BasePreco.agenda_coleta_confirmada.is_(True),
            )
        ).all()
        pendentes = []
        for base in bases:
            resumo = _resumo_calendario_fechamento(db, sub_base, base.base, hoje, hoje)
            if resumo["dias_pendentes"]:
                pendentes.append(base.base)
        if not pendentes:
            continue
        bases_pendentes += len(pendentes)
        por_sub_base[sub_base] = pendentes
        previa = ", ".join(pendentes[:5])
        sufixo = f" e mais {len(pendentes) - 5}" if len(pendentes) > 5 else ""
        notificacoes += send_to_staff_sub_base(
            db,
            sub_base=sub_base,
            tipo="coletas_pendentes",
            title=f"{len(pendentes)} coleta(s) pendente(s)",
            body=f"Faltam lançamentos de hoje: {previa}{sufixo}.",
            data={"screen": "coletas_pendentes", "data": hoje.isoformat()},
        )
    db.commit()
    return {
        "ignorado": False,
        "notificacoes": notificacoes,
        "bases_pendentes": bases_pendentes,
        "por_sub_base": por_sub_base,
    }
