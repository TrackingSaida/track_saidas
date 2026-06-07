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
from address_search_logging import (
    AddressSearchReport,
    GooglePlacesSearchStats,
    ProviderSearchStats,
    best_score_by_source,
    build_provider_stats_list,
    emit_address_search_log,
)
from address_providers.google_places_provider import (
    GooglePlacesClient,
    _record_session_google_call,
    prediction_to_provisional_score,
    should_auto_invoke_google_places,
)
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

PROVIDER_TIMEOUT_SEC = float(os.getenv("ADDRESS_PROVIDER_TIMEOUT_SEC", "2"))
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


def _google_prediction_to_dict(prediction, score: int, search_query: str) -> dict:
    label_parts = [prediction.main_text, prediction.secondary_text]
    label = "\n".join(p for p in label_parts if p) or prediction.full_text
    dist_km = (
        round(prediction.distance_meters / 1000.0, 2)
        if prediction.distance_meters is not None
        else None
    )
    return {
        "label": label,
        "main_text": prediction.main_text,
        "secondary_text": prediction.secondary_text,
        "rua": prediction.main_text,
        "numero": "",
        "bairro": "",
        "cidade": "",
        "estado": "",
        "cep": "",
        "latitude": 0.0,
        "longitude": 0.0,
        "score": score,
        "confidence": round(min(1.0, score / 120.0), 3),
        "source": "google_places",
        "distance_km": dist_km,
        "distance_meters": prediction.distance_meters,
        "place_id": prediction.place_id,
        "requires_place_details": True,
        "badge": None,
        "already_used": False,
    }


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
) -> Tuple[str, List[RawAddressHit], Optional[str]]:
    try:
        return provider.name, provider.search(search_query, latitude, longitude, limit=limit), None
    except Exception as e:
        logger.warning("provider %s failed: %s", provider.name, e)
        return provider.name, [], str(e)


def _fetch_provider_hits(
    providers: List[AddressProvider],
    search_query: str,
    latitude: Optional[float],
    longitude: Optional[float],
    limit: int,
) -> Tuple[List[RawAddressHit], bool, Dict[str, ProviderSearchStats]]:
    if not providers:
        return [], False, {}

    hits: List[RawAddressHit] = []
    timeout = PROVIDER_TIMEOUT_SEC
    timed_out = False
    per_provider: Dict[str, ProviderSearchStats] = {
        p.name: ProviderSearchStats(provider=p.name) for p in providers
    }

    if len(providers) == 1:
        name, provider_hits, error = _run_provider(
            providers[0], search_query, latitude, longitude, limit,
        )
        per_provider[name] = ProviderSearchStats(
            provider=name, results=len(provider_hits), error=error,
        )
        return provider_hits, False, per_provider

    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        futures = {
            pool.submit(_run_provider, p, search_query, latitude, longitude, limit): p
            for p in providers
        }
        try:
            for future in as_completed(futures, timeout=timeout):
                name, provider_hits, error = future.result()
                per_provider[name] = ProviderSearchStats(
                    provider=name, results=len(provider_hits), error=error,
                )
                hits.extend(provider_hits)
        except Exception:
            timed_out = True
            for future, provider in futures.items():
                if future.done():
                    try:
                        name, provider_hits, error = future.result()
                        per_provider[name] = ProviderSearchStats(
                            provider=name, results=len(provider_hits), error=error,
                        )
                        hits.extend(provider_hits)
                    except Exception as e:
                        per_provider[provider.name] = ProviderSearchStats(
                            provider=provider.name, error=str(e),
                        )
                elif per_provider[provider.name].error is None:
                    per_provider[provider.name] = ProviderSearchStats(
                        provider=provider.name, error="timeout",
                    )

    return hits, timed_out, per_provider


def _final_report_fields(suggestions: List[dict]) -> Tuple[Optional[str], int]:
    if not suggestions:
        return None, 0
    return suggestions[0].get("source"), int(suggestions[0].get("score") or 0)


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
        session_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        hints = hints or {}
        normalized_query = normalizeAddressQuery(query)
        search_query = normalized_query or query.strip()
        if len(search_query) < 3:
            log_address_event(db, "address_search_no_results", sub_base, motoboy_id, query)
            emit_address_search_log(
                AddressSearchReport(
                    query=query.strip(),
                    latitude=latitude,
                    longitude=longitude,
                    google=GooglePlacesSearchStats(called=False, reason="query_too_short"),
                    final_results=0,
                    best_score=0,
                )
            )
            return {"suggestions": [], "did_you_mean": None, "used_google": False}

        log_address_event(db, "address_search_started", sub_base, motoboy_id, search_query)

        cached = get_cached(db, sub_base, search_query, latitude, longitude)
        if cached is not None:
            log_address_event(db, "address_search_success", sub_base, motoboy_id, search_query, {"cached": True})
            dym = build_did_you_mean(db, sub_base, search_query, hints=hints)
            cached_slice = cached[:limit]
            final_provider, best_score = _final_report_fields(cached_slice)
            emit_address_search_log(
                AddressSearchReport(
                    query=search_query,
                    latitude=latitude,
                    longitude=longitude,
                    cached=True,
                    final_provider=final_provider or "cache",
                    final_results=len(cached_slice),
                    best_score=best_score,
                )
            )
            return {"suggestions": cached_slice, "did_you_mean": dym, "used_google": False}

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

        providers_timed_out = False
        per_provider_stats: Dict[str, ProviderSearchStats] = {}
        if not skip_providers:
            provider_hits, providers_timed_out, per_provider_stats = _fetch_provider_hits(
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
        used_google = False
        pre_google_best_by_source = best_score_by_source(scored)

        invoke_google, google_reason, cost_guard_hit = should_auto_invoke_google_places(
            suggestions,
            search_query,
            hints=hints,
            providers_timed_out=providers_timed_out,
            session_token=session_token,
        )
        google_stats = GooglePlacesSearchStats(
            called=False,
            reason=google_reason,
            cost_guard_hit=cost_guard_hit,
        )
        if cost_guard_hit:
            log_address_event(
                db,
                "google_places_cost_guard",
                sub_base,
                motoboy_id,
                search_query,
                {
                    "google_places_called": False,
                    "google_places_cost_guard_hit": True,
                    "session_token_prefix": (session_token or "")[:8],
                },
            )
        elif invoke_google:
            _record_session_google_call(session_token)
            client = GooglePlacesClient()
            outcome = client.autocomplete(
                search_query,
                latitude=latitude,
                longitude=longitude,
                session_token=session_token,
                limit=limit,
            )
            google_stats = GooglePlacesSearchStats(
                called=True,
                reason=google_reason,
                http_status=outcome.http_status,
                results=len(outcome.predictions),
                first_result=outcome.first_result,
                error=outcome.error,
            )
            google_suggestions = []
            for pred in outcome.predictions:
                g_score = prediction_to_provisional_score(pred, search_query)
                google_suggestions.append(_google_prediction_to_dict(pred, g_score, search_query))
            merged = _dedupe_hits(suggestions + google_suggestions)
            merged.sort(key=lambda x: (-x["score"], x.get("distance_km") or 9999))
            suggestions = merged[:limit]
            used_google = len(google_suggestions) > 0
            log_address_event(
                db,
                "google_places_autocomplete",
                sub_base,
                motoboy_id,
                search_query,
                {
                    "google_places_called": True,
                    "google_places_reason": google_reason,
                    "google_places_results_count": len(google_suggestions),
                    "google_places_selected": False,
                    "google_places_cost_guard_hit": False,
                    "google_places_http_status": outcome.http_status,
                    "google_places_error": outcome.error,
                },
            )

        if suggestions:
            set_cached(db, sub_base, search_query, latitude, longitude, suggestions)
            log_address_event(
                db, "address_search_success", sub_base, motoboy_id, search_query,
                {
                    "count": len(suggestions),
                    "providers": raw_count,
                    "used_google": used_google,
                },
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

        raw_counts: Dict[str, int] = {"known": len(known_results)}
        for name, stats in per_provider_stats.items():
            raw_counts[name] = stats.results
        provider_errors = {
            name: stats.error for name, stats in per_provider_stats.items() if stats.error
        }
        skipped_external = {p.name for p in self.providers} if skip_providers else set()
        provider_order = ["known"] + [p.name for p in self.providers]
        final_provider, best_score = _final_report_fields(suggestions)
        emit_address_search_log(
            AddressSearchReport(
                query=search_query,
                latitude=latitude,
                longitude=longitude,
                providers=build_provider_stats_list(
                    provider_order,
                    raw_counts,
                    pre_google_best_by_source,
                    provider_errors,
                    skipped=skipped_external,
                ),
                google=google_stats,
                final_provider=final_provider,
                final_results=len(suggestions),
                best_score=best_score,
                providers_timed_out=providers_timed_out,
                skip_external_providers=skip_providers,
            )
        )

        return {"suggestions": suggestions, "did_you_mean": did_you_mean, "used_google": used_google}

    def resolve_place_details(
        self,
        db: Session,
        place_id: str,
        sub_base: str,
        motoboy_id: Optional[int] = None,
        query: str = "",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        hints: Optional[dict] = None,
        session_token: Optional[str] = None,
    ) -> Optional[dict]:
        from address_providers.google_places_provider import is_google_places_enabled, get_google_places_api_key

        if not is_google_places_enabled() or not get_google_places_api_key():
            return None

        client = GooglePlacesClient()
        hit = client.get_place_details(place_id, session_token=session_token)
        if not hit:
            log_address_event(
                db,
                "google_places_details_failed",
                sub_base,
                motoboy_id,
                query or place_id,
                {"place_id": place_id[:32]},
            )
            return None

        sub_cities, sub_bairros = get_sub_base_stats(db, sub_base)
        mot_cities, mot_bairros = get_motoboy_stats(db, motoboy_id) if motoboy_id else ({}, {})
        ctx_base = build_rank_context(
            query or hit.rua,
            hints=hints,
            gps_lat=latitude,
            gps_lon=longitude,
            sub_base_city_weights=sub_cities,
            sub_base_bairro_weights=sub_bairros,
            motoboy_city_weights=mot_cities,
            motoboy_bairro_weights=mot_bairros,
        )
        score, confidence, dist = score_hit(hit, ctx_base)
        suggestion = _hit_to_dict(hit, score, confidence, dist, known_qtd=0)
        suggestion["place_id"] = place_id
        suggestion["main_text"] = hit.rua
        secondary_parts = [p for p in [hit.bairro, hit.cidade, hit.estado] if p]
        suggestion["secondary_text"] = ", ".join(secondary_parts)
        suggestion["requires_place_details"] = False
        if dist < 9000:
            suggestion["distance_meters"] = int(dist * 1000)

        set_cached(db, sub_base, query or hit.rua, latitude, longitude, [suggestion])
        log_address_event(
            db,
            "google_places_details_success",
            sub_base,
            motoboy_id,
            query or hit.rua,
                {
                    "google_places_called": True,
                    "google_places_reason": "place_details",
                    "google_places_results_count": 1,
                    "google_places_selected": True,
                    "google_places_cost_guard_hit": False,
                    "place_id": place_id[:32],
                },
            )
        return suggestion
