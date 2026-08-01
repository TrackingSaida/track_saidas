"""Notificação diária de atraso D+1 para motoboys."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import MotoboySubBase, Saida
from push_notification_service import send_to_motoboy
from saida_operacional_pure import deve_excluir_saida_operacional, timestamp_operacional_saida
from saida_operacional_utils import carregar_contexto_operacional
from saidas_routes import STATUS_EM_ROTA, STATUS_SAIU_PARA_ENTREGA

logger = logging.getLogger(__name__)
TZ = ZoneInfo("America/Sao_Paulo")


def _hoje() -> date:
    return datetime.now(TZ).date()


def notificar_atraso_d1(db: Session) -> Dict[str, int]:
    hoje = _hoje()
    chave = hoje.isoformat()
    vinculos = db.scalars(
        select(MotoboySubBase).where(MotoboySubBase.ativo.is_(True))
    ).all()

    motoboys_notificados = 0
    messages = 0
    seen = set()

    for v in vinculos:
        key = (int(v.motoboy_id), (v.sub_base or "").strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        motoboy_id, sub_base = key

        rows = db.scalars(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.motoboy_id == motoboy_id,
                Saida.codigo.isnot(None),
                Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
            )
        ).all()
        if not rows:
            continue

        ctx_map = carregar_contexto_operacional(db, [s.id_saida for s in rows])
        atraso = 0
        for s in rows:
            ctx = ctx_map.get(s.id_saida)
            if deve_excluir_saida_operacional(ctx):
                continue
            ts = timestamp_operacional_saida(ctx, s.timestamp)
            if ts is None:
                continue
            d = ts.astimezone(TZ).date() if getattr(ts, "tzinfo", None) else ts.date()
            if d < hoje:
                atraso += 1

        if atraso <= 0:
            continue

        n = send_to_motoboy(
            db,
            motoboy_id=motoboy_id,
            sub_base=sub_base,
            tipo="atraso_d1",
            title="Pacotes em atraso",
            body=f"Você tem {atraso} pacote(s) pendente(s) de dias anteriores",
            data={"count": atraso},
            chave_dedupe=chave,
        )
        if n > 0:
            motoboys_notificados += 1
            messages += n

    db.commit()
    return {"motoboys": motoboys_notificados, "messages": messages, "data": chave}
