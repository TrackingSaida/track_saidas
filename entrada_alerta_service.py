"""Alerta periódico: pacotes do dia com entrada na base e ainda sem saída."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from entrada_na_base_utils import contar_ainda_na_base
from models import Owner
from push_notification_service import send_to_staff_sub_base

TZ = ZoneInfo("America/Sao_Paulo")


def _chave_dedupe_slot(agora: datetime) -> str:
    """Arredonda para slot de 5 min BRT — retry no mesmo slot não duplica."""
    minuto = (agora.minute // 5) * 5
    return f"{agora.date().isoformat()}:{agora.hour:02d}:{minuto:02d}"


def notificar_entrada_sem_saida(db: Session) -> dict:
    agora = datetime.now(TZ)
    hoje = agora.date()
    chave = _chave_dedupe_slot(agora)

    owners = db.scalars(
        select(Owner).where(
            Owner.ativo.is_(True),
            Owner.entrada_obrigatoria_habilitada.is_(True),
        )
    ).all()

    notificacoes = 0
    sub_bases_com_pendencia = 0
    por_sub_base: dict = {}

    for owner in owners:
        sub_base = (owner.sub_base or "").strip()
        if not sub_base:
            continue
        count = contar_ainda_na_base(db, sub_base, hoje, hoje)
        if count <= 0:
            continue
        sub_bases_com_pendencia += 1
        por_sub_base[sub_base] = count
        n = send_to_staff_sub_base(
            db,
            sub_base=sub_base,
            tipo="entrada_sem_saida",
            title="Ainda na base",
            body=f"Restam {count} pacote(s) de hoje sem saída",
            data={
                "count": count,
                "data": hoje.isoformat(),
                "screen": "indicadores",
            },
            roles=(0, 1, 2),
            chave_dedupe=chave,
        )
        notificacoes += n

    db.commit()
    return {
        "notificacoes": notificacoes,
        "sub_bases_com_pendencia": sub_bases_com_pendencia,
        "por_sub_base": por_sub_base,
        "data": hoje.isoformat(),
        "chave_dedupe": chave,
    }
