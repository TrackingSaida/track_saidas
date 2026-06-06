"""Orquestrador SmartAddressSearch."""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from address_normalizer import normalize_address_key, normalizeAddressQuery, normalize_cep
from address_providers.base import AddressProvider, RawAddressHit
from address_providers.geoapify_provider import GeoapifyProvider
from address_providers.nominatim_provider import NominatimProvider
from address_ranker import MIN_SUGGESTION_SCORE, RankContext, build_rank_context, score_hit
from address_telemetry import log_address_event
from known_addresses_service import (
    build_did_you_mean,
    build_did_you_mean_from_below_threshold,
    format_suggestion_label,
    search_known,
)
from operational_stats import get_motoboy_stats, get_sub_base_stats
from suggestion_cache import get_cached, set_cached

logger = logging.getLogger(__name__)

PROVIDER_TIMEOUT_SEC = float(os.getenv("ADDRESS_PROVIDER_TIMEOUT_SEC", "3"))
SKIP_PROVIDERS_IF_KNOWN = os.getenv("ADDRESS_SKIP_PROVIDERS_IF_KNOWN", "1").strip() in ("1", "true", "yes")
KNOWN_FAST_SCORE = int(os.getenv("ADDRESS_KNOWN_FAST_SCORE", "40"))


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


def _make_rank_ctx(
    ctx_base: RankContext,
    hints: dict,
    search_query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    sub_cities: dict,
    sub_bairros: dict,
    mot_cities: dict,
    mot_bairros: dict,
    known_qtd: int,
) -> RankContext:
    return RankContext(
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


def _score_all_hits(
    all_hits: List[Tuple[RawAddressHit, int]],
    ctx_base: RankContext,
    hints: dict,
    search_query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    sub_cities: dict,
    sub_bairros: dict,
    mot_cities: dict,
    mot_bairros: dict,
) -> Tuple[List[dict], List[Tuple[RawAddressHit, int, int, float, float]]]:
    scored: List[dict] = []
    below_threshold: List[Tuple[RawAddressHit, int, int, float, float]] = []

    for hit, known_qtd in all_hits:
        if not hit.latitude or not hit.longitude:
            continue
        ctx = _make_rank_ctx(
            ctx_base, hints, search_query, latitude, longitude,
            sub_cities, sub_bairros, mot_cities, mot_bairros, known_qtd,
        )
        score, confidence, dist = score_hit(hit, ctx)
        if score < MIN_SUGGESTION_SCORE:
            below_threshold.append((hit, known_qtd, score, confidence, dist))
            continue
        scored.append(_hit_to_dict(hit, score, confidence, dist, known_qtd))

    return scored, below_threshold


def _run_provider(
    provider: AddressProvider,
    search_query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    limit: int,
) -> List[RawAddressHit]:
    try:
        return provider.search(search_query, latitude, longitude, limit=limit)
    except Exception as e:
        logger.warning("provider %s failed: %s", provider.name, e)
        return []


def _fetch_provider_hits(
    providers: List[AddressProvider],
    search_query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    limit: int,
) -> List[RawAddressHit]:
    if not providers:
        return []

    hits: List[RawAddressHit] = []
    timeout = PROVIDER_TIMEOUT_SEC

    if len(providers) == 1:
        return _run_provider(providers[0], search_query, latitude, longitude, limit)

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = {
            pool.submit(_run_provider, p, search_query, latitude, longitude, limit): p
            for p in providers
        }
        try:
            for future in as_completed(futures, timeout=timeout):
                hits.extend(future.result())
        except Exception:
            for future in futures:
                if future.done():
                    try:
                        hits.extend(future.result())
                    except Exception:
                        pass

    return hits


class SmartAddressSearch:
    def __init__(self) -> None:
        self.providers: List[AddressProvider] = []
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
            dym = build_did_you_mean(db, sub_base, search_query, hints=hints)
            return {"suggestions": cached[:limit], "did_you_mean": dym}

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

        known_results = search_known(db, sub_base, search_query, limit=limit)
        all_hits: List[Tuple[RawAddressHit, int]] = list(known_results)

        skip_providers = False
        if SKIP_PROVIDERS_IF_KNOWN and known_results:
            for hit, qtd in known_results:
                ctx = _make_rank_ctx(
                    ctx_base, hints, search_query, latitude, longitude,
                    sub_cities, sub_bairros, mot_cities, mot_bairros, qtd,
                )
                score, _, _ = score_hit(hit, ctx)
                if score >= KNOWN_FAST_SCORE:
                    skip_providers = True
                    break

        if not skip_providers:
            provider_hits = _fetch_provider_hits(
                self.providers,
                search_query,
                latitude,
                longitude,
                limit,
            )
            for hit in provider_hits:
                all_hits.append((hit, 0))

        raw_count = len(all_hits)
        scored, below_threshold = _score_all_hits(
            all_hits, ctx_base, hints, search_query,
            latitude, longitude, sub_cities, sub_bairros, mot_cities, mot_bairros,
        )

        before_dedup = len(scored)
        scored = _dedupe_hits(scored)
        duplicates_removed = max(0, before_dedup - len(scored))

        if duplicates_removed > 0:
            log_address_event(
                db, "address_duplicate_removed", sub_base, motoboy_id, search_query,
                {"count": duplicates_removed},
            )

        scored.sort(key=lambda x: (-x["score"], x.get("distance_km") or 9999))

        if latitude is not None and longitude is not None and scored:
            best_dist = scored[0].get("distance_km")
            if best_dist is not None and best_dist < 20:
                scored = [
                    s
                    for s in scored
                    if s.get("distance_km") is None
                    or s.get("distance_km", 9999) <= 40
                    or s["score"] >= MIN_SUGGESTION_SCORE + 30
                ]

        suggestions = scored[:limit]

        if suggestions:
            set_cached(db, sub_base, search_query, latitude, longitude, suggestions)
            log_address_event(
                db, "address_search_success", sub_base, motoboy_id, search_query,
                {"count": len(suggestions), "providers": raw_count},
            )
        else:
            log_address_event(db, "address_search_no_results", sub_base, motoboy_id, search_query)

        did_you_mean = build_did_you_mean(
            db, sub_base, search_query, hints=hints, known_hits=known_results or None,
        )
        if not did_you_mean and not suggestions:
            did_you_mean = build_did_you_mean_from_below_threshold(
                below_threshold, search_query, hints, latitude, longitude,
            )

        return {"suggestions": suggestions, "did_you_mean": did_you_mean}
