"""Telemetria operacional de busca de endereços."""
from __future__ import annotations

import hashlib
import logging
import os
import random
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from db_utils import db_rollback_safe

logger = logging.getLogger(__name__)

_SAMPLE_RATE = float(os.getenv("ADDRESS_TELEMETRY_SAMPLE_RATE", "1.0"))


def _query_hash(query: Optional[str]) -> Optional[str]:
    if not query:
        return None
    norm = " ".join(query.strip().lower().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def log_address_event(
    db: Optional[Session],
    event_type: str,
    sub_base: Optional[str] = None,
    motoboy_id: Optional[int] = None,
    query: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        if db is None:
            return
        if _SAMPLE_RATE < 1.0 and random.random() > _SAMPLE_RATE:
            return
        from models import AddressTelemetry

        row = AddressTelemetry(
            event_type=event_type,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
            query_hash=_query_hash(query),
            event_metadata=metadata or {},
        )
        db.add(row)
        db.flush()
    except Exception as e:
        logger.debug("address_telemetry skip: %s (%s)", event_type, e)
        db_rollback_safe(db)
