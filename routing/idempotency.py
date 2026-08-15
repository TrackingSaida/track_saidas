"""Idempotência de otimização via route_optimization_requests."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from models import RouteOptimizationRequest

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def find_by_idempotency_key(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    idempotency_key: str,
) -> Optional["RouteOptimizationRequest"]:
    from models import RouteOptimizationRequest

    return db.query(RouteOptimizationRequest).filter(
        RouteOptimizationRequest.sub_base == sub_base,
        RouteOptimizationRequest.motoboy_id == motoboy_id,
        RouteOptimizationRequest.idempotency_key == idempotency_key,
    ).one_or_none()


def begin_idempotent_request(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    idempotency_key: str,
    request_hash: Optional[str] = None,
    route_id: Optional[int] = None,
) -> Tuple[str, Optional[Dict[str, Any]], Optional["RouteOptimizationRequest"]]:
    """
    Retorna (action, cached_response, row):
    - replay: cached response de completed
    - in_progress: pending existente
    - proceed: nova row pending criada
    """
    from models import RouteOptimizationRequest

    existing = find_by_idempotency_key(
        db, sub_base=sub_base, motoboy_id=motoboy_id, idempotency_key=idempotency_key
    )
    if existing is not None:
        if existing.status == STATUS_COMPLETED and existing.response_json:
            try:
                payload = json.loads(existing.response_json)
            except json.JSONDecodeError:
                payload = None
            return "replay", payload, existing
        if existing.status == STATUS_PENDING:
            return "in_progress", None, existing
        # failed → permite nova tentativa com a mesma key (reabre)
        existing.status = STATUS_PENDING
        existing.request_hash = request_hash
        existing.route_id = route_id
        existing.response_json = None
        existing.completed_at = None
        db.commit()
        db.refresh(existing)
        return "proceed", None, existing

    row = RouteOptimizationRequest(
        sub_base=sub_base,
        motoboy_id=motoboy_id,
        route_id=route_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status=STATUS_PENDING,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return "proceed", None, row
    except IntegrityError:
        db.rollback()
        existing = find_by_idempotency_key(
            db, sub_base=sub_base, motoboy_id=motoboy_id, idempotency_key=idempotency_key
        )
        if existing and existing.status == STATUS_COMPLETED and existing.response_json:
            return "replay", json.loads(existing.response_json), existing
        if existing and existing.status == STATUS_PENDING:
            return "in_progress", None, existing
        raise


def complete_idempotent_request(
    db: Session,
    row: "RouteOptimizationRequest",
    *,
    response: Dict[str, Any],
    route_id: Optional[int] = None,
    route_revision: Optional[int] = None,
) -> None:
    row.status = STATUS_COMPLETED
    row.response_json = json.dumps(response, ensure_ascii=False)
    row.completed_at = datetime.now(timezone.utc)
    if route_id is not None:
        row.route_id = route_id
    if route_revision is not None:
        row.route_revision = route_revision
    db.commit()


def fail_idempotent_request(
    db: Session,
    row: Optional["RouteOptimizationRequest"],
    *,
    error: Dict[str, Any],
) -> None:
    if row is None:
        return
    row.status = STATUS_FAILED
    row.response_json = json.dumps({"error": error}, ensure_ascii=False)
    row.completed_at = datetime.now(timezone.utc)
    db.commit()
