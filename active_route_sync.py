"""
Sincroniza rota ativa do motoboy após finalização de entrega (entregue/ausente).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import RotasMotoboy, Saida
from saidas_routes import STATUS_AUSENTE, STATUS_ENTREGUE, normalizar_status_saida, _hoje_operacional

logger = logging.getLogger(__name__)


def _status_upper(status: Optional[str]) -> str:
    return (normalizar_status_saida(status) or "").strip().upper()


def get_active_route_delivery_ids(
    db: Session,
    motoboy_id: int,
    *,
    hoje: Optional[date] = None,
) -> Optional[List[int]]:
    """Retorna ids da ordem da rota ativa ou None se não houver rota ativa."""
    ref_date = hoje or _hoje_operacional()
    rota = db.scalar(
        select(RotasMotoboy)
        .where(
            RotasMotoboy.motoboy_id == motoboy_id,
            RotasMotoboy.status == "ativa",
            RotasMotoboy.data == ref_date,
            RotasMotoboy.finalizado_em.is_(None),
        )
        .order_by(RotasMotoboy.iniciado_em.desc())
        .limit(1)
    )
    if not rota:
        return None
    ordem_raw = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    return [int(x) for x in (ordem_raw or [])]


def _ordem_from_rota(rota: RotasMotoboy) -> List[int]:
    ordem_raw = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    return [int(x) for x in (ordem_raw or [])]


def _recalc_parada_and_maybe_finalize(
    db: Session,
    rota: RotasMotoboy,
    ordem: List[int],
    *,
    log_context: str = "",
) -> bool:
    """
    Atualiza parada_atual e finaliza a rota se todos os pedidos da ordem estiverem
    entregues ou ausentes. Retorna True se a rota permanece ativa.
    """
    if not ordem:
        return False

    rows = db.scalars(select(Saida).where(Saida.id_saida.in_(ordem))).all()
    status_by_id = {int(s.id_saida): _status_upper(s.status) for s in rows}

    def is_finalized(sid: int) -> bool:
        return status_by_id.get(sid, "") in (STATUS_ENTREGUE, STATUS_AUSENTE)

    first_pending = len(ordem)
    for i, sid in enumerate(ordem):
        if not is_finalized(sid):
            first_pending = i
            break

    rota.parada_atual = first_pending

    if first_pending >= len(ordem):
        rota.status = "finalizada"
        rota.finalizado_em = datetime.utcnow()
        logger.info(
            "active_route_sync rota_finalizada rota_id=%s%s",
            rota.id,
            f" {log_context}" if log_context else "",
        )
        db.commit()
        db.refresh(rota)
        return False

    if log_context:
        logger.info(
            "active_route_sync parada_atual=%s rota_id=%s %s",
            first_pending,
            rota.id,
            log_context,
        )
    db.commit()
    db.refresh(rota)
    return True


def refresh_active_route_if_stale(db: Session, rota: RotasMotoboy) -> bool:
    """Recalcula progresso da rota ativa; finaliza se todos os pedidos já foram concluídos."""
    ordem = _ordem_from_rota(rota)
    return _recalc_parada_and_maybe_finalize(db, rota, ordem, log_context="refresh_rotas_ativa")


def sync_active_route_after_delivery_update(
    db: Session,
    motoboy_id: int,
    id_saida: int,
    *,
    hoje: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Recalcula parada_atual da rota ativa e finaliza a rota se todos os pedidos
    da ordem estiverem entregues ou ausentes.
    """
    ref_date = hoje or _hoje_operacional()
    rota = db.scalar(
        select(RotasMotoboy)
        .where(
            RotasMotoboy.motoboy_id == motoboy_id,
            RotasMotoboy.status == "ativa",
            RotasMotoboy.data == ref_date,
            RotasMotoboy.finalizado_em.is_(None),
        )
        .order_by(RotasMotoboy.iniciado_em.desc())
        .limit(1)
    )
    if not rota:
        return {"in_active_route": False, "rota_finalizada": False}

    ordem_raw = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    ordem: List[int] = [int(x) for x in (ordem_raw or [])]
    if int(id_saida) not in ordem:
        return {"in_active_route": False, "rota_finalizada": False}

    still_active = _recalc_parada_and_maybe_finalize(
        db,
        rota,
        ordem,
        log_context=f"motoboy_id={motoboy_id} trigger_id_saida={id_saida}",
    )
    rota_finalizada = not still_active

    return {
        "in_active_route": still_active,
        "rota_id": str(rota.id),
        "parada_atual": int(rota.parada_atual or 0),
        "ordem": ordem,
        "rota_finalizada": rota_finalizada,
    }
