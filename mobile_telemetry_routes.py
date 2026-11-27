"""POST /mobile/telemetry/events — funil de baixa (PRD-018).

Piloto: só persiste para sub_bases na allowlist (default GG FLEX).
Tenant (motoboy_id / sub_base / owner_id) vem só do token.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user, ensure_motoboy_session
from db import get_db
from mobile_telemetry_policy import (
    clip_text,
    is_allowed_event_name,
    sanitize_props,
    telemetry_sub_base_allowlist,
)
from models import MobileTelemetryEvent, Owner, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mobile/telemetry", tags=["Mobile Telemetry"])


def get_current_motoboy_telemetry(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    return ensure_motoboy_session(db, user)


def _parse_event_at(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _resolve_owner_id(db: Session, sub_base: str) -> Optional[int]:
    row = db.scalar(
        select(Owner.id_owner).where(Owner.sub_base == sub_base).limit(1)
    )
    try:
        return int(row) if row is not None else None
    except (TypeError, ValueError):
        return None


class TelemetryEventIn(BaseModel):
    event_name: str = Field(..., max_length=64)
    event_at: Optional[str] = Field(None, max_length=64)
    id_saida: Optional[int] = None
    client_action_id: Optional[str] = Field(None, max_length=64)
    duration_ms: Optional[int] = Field(None, ge=0, le=3_600_000)
    stage: Optional[str] = Field(None, max_length=64)
    app_version: Optional[str] = Field(None, max_length=32)
    platform: Optional[str] = Field(None, max_length=32)
    device_brand: Optional[str] = Field(None, max_length=64)
    device_manufacturer: Optional[str] = Field(None, max_length=64)
    device_model: Optional[str] = Field(None, max_length=96)
    os_name: Optional[str] = Field(None, max_length=32)
    os_version: Optional[str] = Field(None, max_length=32)
    android_api_level: Optional[int] = Field(None, ge=1, le=99)
    device_total_memory_mb: Optional[int] = Field(None, ge=0, le=1_000_000)
    cpu_arch: Optional[str] = Field(None, max_length=64)
    props: Optional[dict] = None


class TelemetryBatchIn(BaseModel):
    events: list[TelemetryEventIn] = Field(..., min_length=1, max_length=50)


class TelemetryBatchOut(BaseModel):
    accepted: int
    skipped: int
    reason: Optional[str] = None


def _is_valid_event(item: TelemetryEventIn) -> bool:
    return is_allowed_event_name(item.event_name)


@router.post("/events", response_model=TelemetryBatchOut)
def post_telemetry_events(
    payload: TelemetryBatchIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_motoboy_telemetry),
):
    sub_base = (getattr(current_user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(status_code=400, detail="sub_base não definida na sessão.")

    try:
        motoboy_id = int(getattr(current_user, "motoboy_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Token inválido para motoboy.")

    allowlist = telemetry_sub_base_allowlist()
    if sub_base not in allowlist:
        return TelemetryBatchOut(
            accepted=0,
            skipped=len(payload.events),
            reason="sub_base_not_in_pilot",
        )

    valid_items = [item for item in payload.events if _is_valid_event(item)]
    if not valid_items:
        raise HTTPException(
            status_code=422,
            detail={"code": "TELEMETRY_EVENTS_INVALID", "message": "Nenhum evento válido."},
        )

    owner_id = _resolve_owner_id(db, sub_base)
    rows: list[MobileTelemetryEvent] = []
    for item in valid_items:
        rows.append(
            MobileTelemetryEvent(
                event_at=_parse_event_at(item.event_at),
                event_name=(item.event_name or "").strip(),
                motoboy_id=motoboy_id,
                sub_base=sub_base,
                owner_id=owner_id,
                id_saida=item.id_saida,
                client_action_id=clip_text(item.client_action_id, 64),
                duration_ms=item.duration_ms,
                stage=clip_text(item.stage, 64),
                app_version=clip_text(item.app_version, 32),
                platform=clip_text(item.platform, 32),
                device_brand=clip_text(item.device_brand, 64),
                device_manufacturer=clip_text(item.device_manufacturer, 64),
                device_model=clip_text(item.device_model, 96),
                os_name=clip_text(item.os_name, 32),
                os_version=clip_text(item.os_version, 32),
                android_api_level=item.android_api_level,
                device_total_memory_mb=item.device_total_memory_mb,
                cpu_arch=clip_text(item.cpu_arch, 64),
                props_json=sanitize_props(item.props),
            )
        )

    skipped = len(payload.events) - len(rows)
    try:
        db.add_all(rows)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("mobile_telemetry_insert_failed motoboy_id=%s", motoboy_id)
        raise HTTPException(status_code=500, detail="Falha ao gravar telemetria.")

    return TelemetryBatchOut(accepted=len(rows), skipped=skipped)
