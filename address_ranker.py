"""Ranking contextual de sugestões de endereço."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, Optional

from address_normalizer import normalize_address_key, normalize_numero_part, normalize_street_part
from address_providers.base import RawAddressHit
from address_normalizer import normalize_cep

MIN_SUGGESTION_SCORE = int(os.getenv("MIN_SUGGESTION_SCORE", "20"))
MAX_SCORE_REFERENCE = 250.0


@dataclass
class RankContext:
    query: str
    query_numero: str = ""
    query_cep: str = ""
    query_rua_norm: str = ""
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    sub_base_city_weights: Optional[Dict[str, int]] = None
    sub_base_bairro_weights: Optional[Dict[str, int]] = None
    motoboy_city_weights: Optional[Dict[str, int]] = None
    motoboy_bairro_weights: Optional[Dict[str, int]] = None
    known_qtd: int = 0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _gps_score(distance_km: Optional[float]) -> int:
    if distance_km is None:
        return 0
    if distance_km <= 3:
        return 100
    if distance_km <= 10:
        return 80
    if distance_km <= 20:
        return 50
    if distance_km <= 40:
        return 20
    return 0


def _cep_score(query_cep: str, hit_cep: str) -> int:
    if not query_cep or not hit_cep:
        return 0
    if query_cep == hit_cep:
        return 50
    if len(query_cep) >= 5 and len(hit_cep) >= 5 and query_cep[:5] == hit_cep[:5]:
        return 20
    if query_cep[:3] == hit_cep[:3]:
        return 0
    return -50


def _numero_score(query_num: str, hit_num: str) -> int:
    if not query_num:
        return 0
    hit_n = normalize_numero_part(hit_num)
    if hit_n == query_num:
        return 40
    if hit_n and hit_n != query_num:
        return -20
    return 0


def _recurrence_bonus(weights: Optional[Dict[str, int]], key: str, cap: int = 20) -> int:
    if not weights or not key:
        return 0
    norm = normalize_street_part(key)
    count = weights.get(norm, 0)
    if count <= 0:
        return 0
    return min(cap, 5 + int(math.log1p(count) * 5))


def score_hit(hit: RawAddressHit, ctx: RankContext) -> tuple[int, float, float]:
    if not hit.latitude or not hit.longitude:
        return -100, 0.0, 9999.0

    distance_km: Optional[float] = None
    if ctx.gps_lat is not None and ctx.gps_lon is not None:
        distance_km = haversine_km(ctx.gps_lat, ctx.gps_lon, hit.latitude, hit.longitude)

    score = 0
    score += _gps_score(distance_km)

    hit_cep = normalize_cep(hit.cep)
    score += _cep_score(ctx.query_cep, hit_cep)
    score += _numero_score(ctx.query_numero, hit.numero)

    hit_rua_norm = normalize_street_part(hit.rua)
    if ctx.query_rua_norm and hit_rua_norm:
        if ctx.query_rua_norm == hit_rua_norm:
            score += 10
        elif ctx.query_rua_norm in hit_rua_norm or hit_rua_norm in ctx.query_rua_norm:
            score += 5

    if ctx.known_qtd > 0:
        score += min(30, 10 + int(math.log1p(ctx.known_qtd) * 8))

    if hit.source == "google_places":
        score += 10

    score += _recurrence_bonus(ctx.sub_base_city_weights, hit.cidade, 20)
    score += _recurrence_bonus(ctx.sub_base_bairro_weights, hit.bairro, 20)
    score += _recurrence_bonus(ctx.motoboy_city_weights, hit.cidade, 25)
    score += _recurrence_bonus(ctx.motoboy_bairro_weights, hit.bairro, 15)

    if not hit.cidade or not hit.estado or len(hit.estado) < 2:
        score -= 30

    if distance_km is not None and distance_km > 40:
        score -= 50

    confidence = max(0.0, min(1.0, score / MAX_SCORE_REFERENCE))
    dist_out = distance_km if distance_km is not None else 9999.0
    return score, confidence, dist_out


def build_rank_context(
    query: str,
    hints: Optional[dict] = None,
    gps_lat: Optional[float] = None,
    gps_lon: Optional[float] = None,
    **kwargs,
) -> RankContext:
    hints = hints or {}
    import re

    nums = re.findall(r"\d+", query or "")
    query_num = normalize_numero_part(hints.get("numero") or (nums[-1] if nums else ""))
    query_cep = normalize_cep(hints.get("cep") or "")
    rua_hint = hints.get("rua") or query
    rua_norm = normalize_street_part(rua_hint)
    return RankContext(
        query=query,
        query_numero=query_num,
        query_cep=query_cep,
        query_rua_norm=rua_norm,
        gps_lat=gps_lat,
        gps_lon=gps_lon,
        **kwargs,
    )
