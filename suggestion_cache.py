"""Cache de sugestões de endereço."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TTL_DAYS = int(os.getenv("SUGGESTION_CACHE_TTL_DAYS", "7"))
_memory: Dict[str, tuple] = {}
_table_available: Optional[bool] = None


def _normalize_query(query: str) -> str:
    return " ".join((query or "").strip().lower().split())


def _key_hash(sub_base: str, query: str, lat: Optional[float], lon: Optional[float]) -> str:
    lat_r = round(lat, 2) if lat is not None else "na"
    lon_r = round(lon, 2) if lon is not None else "na"
    raw = f"{sub_base}|{_normalize_query(query)}|{lat_r}|{lon_r}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_table_available(db: Session) -> bool:
    global _table_available
    if _table_available is not None:
        return _table_available
    try:
        from models import SuggestionCache  # noqa: F401

        db.execute(select(SuggestionCache).limit(1))
        _table_available = True
    except Exception:
        _table_available = False
        logger.warning("suggestion_cache: tabela indisponível, usando memória")
    return _table_available


def get_cached(
    db: Optional[Session],
    sub_base: str,
    query: str,
    lat: Optional[float],
    lon: Optional[float],
) -> Optional[List[Dict[str, Any]]]:
    key = _key_hash(sub_base, query, lat, lon)
    if db and _is_table_available(db):
        try:
            from models import SuggestionCache

            row = db.execute(select(SuggestionCache).where(SuggestionCache.key_hash == key)).scalar_one_or_none()
            if not row:
                return None
            cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if updated < cutoff:
                db.delete(row)
                db.flush()
                return None
            return json.loads(row.payload_json)
        except Exception as e:
            logger.warning("suggestion_cache get db error: %s", e)
    entry = _memory.get(key)
    if not entry:
        return None
    expires_at, payload = entry
    if expires_at < datetime.now(timezone.utc).timestamp():
        _memory.pop(key, None)
        return None
    return payload


def set_cached(
    db: Optional[Session],
    sub_base: str,
    query: str,
    lat: Optional[float],
    lon: Optional[float],
    suggestions: List[Dict[str, Any]],
) -> None:
    key = _key_hash(sub_base, query, lat, lon)
    payload_json = json.dumps(suggestions, ensure_ascii=False)
    if db and _is_table_available(db):
        try:
            from models import SuggestionCache

            row = db.execute(select(SuggestionCache).where(SuggestionCache.key_hash == key)).scalar_one_or_none()
            if row:
                row.payload_json = payload_json
                row.updated_at = datetime.now(timezone.utc)
            else:
                row = SuggestionCache(
                    key_hash=key,
                    sub_base=sub_base,
                    query_normalizada=_normalize_query(query),
                    payload_json=payload_json,
                    hit_count=0,
                )
                db.add(row)
            db.flush()
            return
        except Exception as e:
            logger.warning("suggestion_cache set db error: %s", e)
    expires = datetime.now(timezone.utc).timestamp() + TTL_DAYS * 86400
    _memory[key] = (expires, suggestions)
