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

MOTIVO_ROTA_SEM_ITENS = "SEM_ITENS_EXECUTAVEIS"


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


def remove_delivery_from_open_routes(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: Optional[int],
    id_saida: int,
) -> Dict[str, Any]:
    """Remove um item reatribuído/cancelado das rotas aberta e pronta.

    Não faz commit: deve participar da mesma transação da alteração operacional.
    """
    if motoboy_id is None:
        return {"removed": False}
    rotas = list(
        db.scalars(
            select(RotasMotoboy)
            .where(
                RotasMotoboy.sub_base == sub_base,
                RotasMotoboy.motoboy_id == motoboy_id,
                RotasMotoboy.status.in_(["preparando", "ativa"]),
                RotasMotoboy.finalizado_em.is_(None),
            )
            .with_for_update()
        ).all()
    )
    removidas: List[str] = []
    encerradas: List[str] = []
    now = datetime.utcnow()
    for rota in rotas:
        ordem = _ordem_from_rota(rota)
        if int(id_saida) not in ordem:
            continue
        idx = ordem.index(int(id_saida))
        parada = int(rota.parada_atual or 0)
        if idx < parada:
            parada -= 1
        ordem = [sid for sid in ordem if sid != int(id_saida)]
        if parada < 0:
            parada = 0
        if ordem and parada > len(ordem):
            parada = len(ordem)
        if not ordem:
            parada = 0
        rota.ordem_json = json.dumps(ordem)
        rota.route_revision = int(getattr(rota, "route_revision", 0) or 0) + 1
        rota.updated_at = now
        rota.geometry_status = "stale" if getattr(rota, "polyline_encoded", None) else "missing"
        rota.geometry_order_hash = None
        rota.parada_atual = parada
        if not ordem:
            rota.motivo_encerramento = MOTIVO_ROTA_SEM_ITENS
            rota.finalizado_em = now
            if rota.status == "ativa":
                rota.status = "finalizada"
            else:
                rota.status = "cancelada"
            logger.info(
                "active_route_sync rota_sem_itens rota_id=%s status=%s motivo=%s",
                rota.id,
                rota.status,
                MOTIVO_ROTA_SEM_ITENS,
            )
            encerradas.append(str(rota.id))
        removidas.append(str(rota.id))
    return {
        "removed": bool(removidas),
        "route_ids": removidas,
        "rotas_encerradas": encerradas,
        "motivo_sistema": MOTIVO_ROTA_SEM_ITENS if encerradas else None,
        "inserido_em_outra_rota": False,
    }


def encerrar_item_da_operacao_anterior(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: Optional[int],
    id_saida: int,
    motivo: str,
    user_id: Optional[int],
) -> Dict[str, Any]:
    """Invalida a sessão viva e tira o item da rota aberta do motoboy anterior.

    Não inclui o pedido na rota de outro motoboy e não marca conferência como Reconferir.
    """
    from saida_sessao_service import invalidar_vinculo_operacional

    vinculos = invalidar_vinculo_operacional(
        db,
        id_saida=int(id_saida),
        motivo=motivo,
        user_id=user_id,
    )
    rota = remove_delivery_from_open_routes(
        db,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
        id_saida=int(id_saida),
    )
    return {"vinculos_invalidados": vinculos, **rota}
