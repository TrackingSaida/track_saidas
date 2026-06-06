"""Orquestrador SmartAddressSearch."""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from address_normalizer import normalize_address_key, normalizeAddressQuery
from address_providers.base import RawAddressHit
from address_providers.geoapify_provider import GeoapifyProvider
from address_providers.nominatim_provider import NominatimProvider
from address_ranker import MIN_SUGGESTION_SCORE, RankContext, build_rank_context, score_hit
from address_telemetry import log_address_event
from address_normalizer import normalize_cep
from known_addresses_service import format_suggestion_label, search_known, build_did_you_mean, _format_suggestion_dict
from operational_stats import get_motoboy_stats, get_sub_base_stats
from suggestion_cache import get_cached, set_cached

logger = logging.getLogger(__name__)


def _extract_numero_from_query(query: str) -> str:
    nums = re.findall(r"\d+", query or "")
    return nums[-1] if nums else ""


def _dedupe_hits(scored: List[dict]) -> List[dict]:
    seen: Dict[str, dict] = {}
    for item in scored:
        key = normalize_address_key(item.get("rua"), item.get("numero"), item.get("cep"))
        prev = seen.get(key)
        if prev is None or item.get("score", 0) > prev.get("score", 0):
            seen[key] = item
    return list(seen.values())


def _hit_to_dict(hit: RawAddressHit, score: int, confidence: float, distance_km: float, known_qtd: int = 0) -> dict:
    badge = None
    already_used = known_qtd > 0
    if known_qtd >= 10:
        badge = "frequente"
    elif already_used:
        badge = "used"
    dist = distance_km if distance_km < 9000 else None
    return {
        "label": format_suggestion_label(hit, dist),
        "rua": hit.rua,
        "numero": hit.numero,
        "bairro": hit.bairro,
        "cidade": hit.cidade,
        "estado": hit.estado,
        "cep": hit.cep,
        "latitude": hit.latitude,
        "longitude": hit.longitude,
        "score": score,
        "confidence": round(confidence, 3),
        "source": hit.source,
        "distance_km": round(dist, 2) if dist is not None else None,
        "badge": badge,
        "already_used": already_used,
    }


class SmartAddressSearch:
    def __init__(self) -> None:
        self.providers = []
        provider_names = os.getenv("ADDRESS_PROVIDERS", "geoapify,nominatim").split(",")
        for name in provider_names:
            n = name.strip().lower()
            if n == "geoapify":
                self.providers.append(GeoapifyProvider())
            elif n == "nominatim":
                self.providers.append(NominatimProvider())

    def search(
        self,
        db: Session,
        query: str,
        sub_base: str,
        motoboy_id: Optional[int] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        hints: Optional[dict] = None,
        limit: int = 8,
    ) -> Dict[str, Any]:
        hints = hints or {}
        normalized_query = normalizeAddressQuery(query)
        search_query = normalized_query or query.strip()
        if len(search_query) < 3:
            log_address_event(db, "address_search_no_results", sub_base, motoboy_id, query)
            return {"suggestions": [], "did_you_mean": None}

        log_address_event(db, "address_search_started", sub_base, motoboy_id, search_query)

        cached = get_cached(db, sub_base, search_query, latitude, longitude)
        if cached is not None:
            log_address_event(db, "address_search_success", sub_base, motoboy_id, search_query, {"cached": True})
            return {"suggestions": cached[:limit], "did_you_mean": build_did_you_mean(db, sub_base, search_query)}

        sub_cities, sub_bairros = get_sub_base_stats(db, sub_base)
        mot_cities, mot_bairros = get_motoboy_stats(db, motoboy_id) if motoboy_id else ({}, {})

        ctx_base = build_rank_context(
            search_query,
            hints=hints,
            gps_lat=latitude,
            gps_lon=longitude,
            sub_base_city_weights=sub_cities,
            sub_base_bairro_weights=sub_bairros,
            motoboy_city_weights=mot_cities,
            motoboy_bairro_weights=mot_bairros,
        )

        all_hits: List[tuple[RawAddressHit, int]] = []

        for hit, qtd in search_known(db, sub_base, search_query, limit=limit):
            all_hits.append((hit, qtd))

        for provider in self.providers:
            try:
                for hit in provider.search(search_query, latitude, longitude, limit=limit):
                    all_hits.append((hit, 0))
            except Exception as e:
                logger.warning("provider %s failed: %s", provider.name, e)

        scored: List[dict] = []
        duplicates_removed = 0
        raw_count = len(all_hits)

        for hit, known_qtd in all_hits:
            if not hit.latitude or not hit.longitude:
                continue
            ctx = RankContext(
                query=ctx_base.query,
                query_numero=ctx_base.query_numero or _extract_numero_from_query(search_query),
                query_cep=ctx_base.query_cep or normalize_cep(hints.get("cep")),
                query_rua_norm=ctx_base.query_rua_norm,
                gps_lat=latitude,
                gps_lon=longitude,
                sub_base_city_weights=sub_cities,
                sub_base_bairro_weights=sub_bairros,
                motoboy_city_weights=mot_cities,
                motoboy_bairro_weights=mot_bairros,
                known_qtd=known_qtd,
            )
            score, confidence, dist = score_hit(hit, ctx)
            if score < MIN_SUGGESTION_SCORE:
                continue
            scored.append(_hit_to_dict(hit, score, confidence, dist, known_qtd))

        before_dedup = len(scored)
        scored = _dedupe_hits(scored)
        duplicates_removed = max(0, before_dedup - len(scored))

        if duplicates_removed > 0:
            log_address_event(
                db,
                "address_duplicate_removed",
                sub_base,
                motoboy_id,
                search_query,
                {"count": duplicates_removed},
            )

        scored.sort(key=lambda x: (-x["score"], x.get("distance_km") or 9999))

        if latitude is not None and longitude is not None and scored:
            best_dist = scored[0].get("distance_km")
            if best_dist is not None and best_dist < 20:
                scored = [
                    s
                    for s in scored
                    if s.get("distance_km") is None or s.get("distance_km", 9999) <= 40 or s["score"] >= MIN_SUGGESTION_SCORE + 30
                ]

        suggestions = scored[:limit]

        if suggestions:
            set_cached(db, sub_base, search_query, latitude, longitude, suggestions)
            log_address_event(
                db,
                "address_search_success",
                sub_base,
                motoboy_id,
                search_query,
                {"count": len(suggestions), "providers": raw_count},
            )
        else:
            log_address_event(db, "address_search_no_results", sub_base, motoboy_id, search_query)

        did_you_mean = build_did_you_mean(db, sub_base, search_query)
        return {"suggestions": suggestions, "did_you_mean": did_you_mean}
