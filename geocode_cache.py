"""
Cache de geocoding com TTL de 30 dias.
Fallback em memória quando a tabela ainda não foi migrada.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TTL_DAYS = 30
_MEMORY_TTL_SEC = TTL_DAYS * 86400


@dataclass
class _MemoryEntry:
    latitude: float
    longitude: float
    provider: Optional[str]
    confidence: Optional[float]
    expires_at: float


_memory_cache: Dict[str, _MemoryEntry] = {}
_table_available: Optional[bool] = None


def _normalize_query(query: str) -> str:
    return " ".join((query or "").strip().lower().split())


def _key_hash(query: str) -> str:
    norm = _normalize_query(query)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _is_table_available(db: Session) -> bool:
    global _table_available
    if _table_available is not None:
        return _table_available
    try:
        from models import GeocodeCache  # noqa: F401

        db.execute(select(GeocodeCache).limit(1))
        _table_available = True
    except Exception:
        _table_available = False
        logger.warning("geocode_cache: tabela indisponível, usando cache em memória")
    return _table_available


def get_cached(db: Optional[Session], query: str) -> Optional[Tuple[float, float, Optional[str]]]:
    """Retorna (lat, lon, provider) ou None."""
    norm = _normalize_query(query)
    if not norm:
        return None

    key = _key_hash(norm)

    if db is not None and _is_table_available(db):
        try:
            from models import GeocodeCache

            row = db.scalar(select(GeocodeCache).where(GeocodeCache.key_hash == key))
            if row is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
                updated = row.updated_at
                if updated is not None and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated is not None and updated < cutoff:
                    db.delete(row)
                    db.commit()
                    return None
                row.hit_count = (row.hit_count or 0) + 1
                row.updated_at = datetime.now(timezone.utc)
                db.commit()
                logger.info("geocode_attempt cache_hit=true query=%s", norm[:80])
                return float(row.latitude), float(row.longitude), row.provider
        except Exception as e:
            logger.warning("geocode_cache get_cached falhou: %s", e)

    entry = _memory_cache.get(key)
    if entry and entry.expires_at > time.time():
        logger.info("geocode_attempt cache_hit=true memory=true query=%s", norm[:80])
        return entry.latitude, entry.longitude, entry.provider
    if entry:
        _memory_cache.pop(key, None)
    return None


def set_cached(
    db: Optional[Session],
    query: str,
    latitude: float,
    longitude: float,
    provider: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    norm = _normalize_query(query)
    if not norm:
        return
    key = _key_hash(norm)

    if db is not None and _is_table_available(db):
        try:
            from models import GeocodeCache

            row = db.scalar(select(GeocodeCache).where(GeocodeCache.key_hash == key))
            now = datetime.now(timezone.utc)
            if row is None:
                row = GeocodeCache(
                    key_hash=key,
                    query_normalizada=norm,
                    latitude=latitude,
                    longitude=longitude,
                    provider=provider,
                    confidence=confidence,
                    hit_count=0,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.latitude = latitude
                row.longitude = longitude
                row.provider = provider
                row.confidence = confidence
                row.updated_at = now
            db.commit()
            return
        except Exception as e:
            logger.warning("geocode_cache set_cached falhou: %s", e)

    _memory_cache[key] = _MemoryEntry(
        latitude=latitude,
        longitude=longitude,
        provider=provider,
        confidence=confidence,
        expires_at=time.time() + _MEMORY_TTL_SEC,
    )
