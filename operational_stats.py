"""Estatísticas operacionais por sub-base e motoboy."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from address_normalizer import normalize_street_part
from db_utils import db_rollback_safe

logger = logging.getLogger(__name__)
from models import EnderecoConhecido, SaidaDetail

_STATS_TTL_SEC = 300
_sub_base_cache: Dict[str, Tuple[float, Tuple[Dict[str, int], Dict[str, int]]]] = {}
_motoboy_cache: Dict[str, Tuple[float, Tuple[Dict[str, int], Dict[str, int]]]] = {}


def _count_by_field(rows, field: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for val, cnt in rows:
        if not val:
            continue
        key = normalize_street_part(str(val))
        out[key] = int(cnt)
    return out


def _load_sub_base_stats(db: Session, sub_base: str) -> Tuple[Dict[str, int], Dict[str, int]]:
    city_rows = db.execute(
        select(EnderecoConhecido.cidade, func.count())
        .where(EnderecoConhecido.sub_base == sub_base)
        .group_by(EnderecoConhecido.cidade)
        .order_by(func.count().desc())
        .limit(30)
    ).all()
    bairro_rows = db.execute(
        select(EnderecoConhecido.bairro, func.count())
        .where(EnderecoConhecido.sub_base == sub_base, EnderecoConhecido.bairro.isnot(None))
        .group_by(EnderecoConhecido.bairro)
        .order_by(func.count().desc())
        .limit(50)
    ).all()
    if not city_rows:
        city_rows = db.execute(
            select(SaidaDetail.dest_cidade, func.count())
            .where(SaidaDetail.dest_cidade.isnot(None))
            .group_by(SaidaDetail.dest_cidade)
            .order_by(func.count().desc())
            .limit(20)
        ).all()
    return _count_by_field(city_rows, "cidade"), _count_by_field(bairro_rows, "bairro")


def get_sub_base_stats(db: Session, sub_base: str) -> Tuple[Dict[str, int], Dict[str, int]]:
    now = time.time()
    cached = _sub_base_cache.get(sub_base)
    if cached and now - cached[0] < _STATS_TTL_SEC:
        return cached[1]
    try:
        result = _load_sub_base_stats(db, sub_base)
    except Exception as e:
        db_rollback_safe(db)
        logger.warning("get_sub_base_stats failed sub_base=%s: %s", sub_base, e)
        return {}, {}
    _sub_base_cache[sub_base] = (now, result)
    return result


def _load_motoboy_stats(db: Session, motoboy_id: int, days: int = 30) -> Tuple[Dict[str, int], Dict[str, int]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    from models import Saida

    city_rows = db.execute(
        select(SaidaDetail.dest_cidade, func.count())
        .join(Saida, Saida.id_saida == SaidaDetail.id_saida)
        .where(Saida.motoboy_id == motoboy_id, Saida.data_hora_entrega >= cutoff)
        .group_by(SaidaDetail.dest_cidade)
        .order_by(func.count().desc())
        .limit(20)
    ).all()
    bairro_rows = db.execute(
        select(SaidaDetail.dest_bairro, func.count())
        .join(Saida, Saida.id_saida == SaidaDetail.id_saida)
        .where(
            Saida.motoboy_id == motoboy_id,
            Saida.data_hora_entrega >= cutoff,
            SaidaDetail.dest_bairro.isnot(None),
        )
        .group_by(SaidaDetail.dest_bairro)
        .order_by(func.count().desc())
        .limit(30)
    ).all()
    return _count_by_field(city_rows, "cidade"), _count_by_field(bairro_rows, "bairro")


def get_motoboy_stats(db: Session, motoboy_id: int, days: int = 30) -> Tuple[Dict[str, int], Dict[str, int]]:
    cache_key = f"{motoboy_id}:{days}"
    now = time.time()
    cached = _motoboy_cache.get(cache_key)
    if cached and now - cached[0] < _STATS_TTL_SEC:
        return cached[1]
    try:
        result = _load_motoboy_stats(db, motoboy_id, days)
    except Exception as e:
        db_rollback_safe(db)
        logger.warning("get_motoboy_stats failed motoboy_id=%s: %s", motoboy_id, e)
        return {}, {}
    _motoboy_cache[cache_key] = (now, result)
    return result
