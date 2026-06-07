"""Google Places API (New) — autocomplete e place details."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from address_normalizer import normalize_cep, normalize_estado_uf
from address_providers.base import AddressProvider, RawAddressHit

logger = logging.getLogger(__name__)

_AUTOCOMPLETE_URL = "https://places.googleapis.com/v1/places:autocomplete"
_PLACES_BASE_URL = "https://places.googleapis.com/v1/places"
_USER_AGENT = "TrackSaidasBackend/1.0"

_AUTOCOMPLETE_FIELD_MASK = (
    "suggestions.placePrediction.placeId,"
    "suggestions.placePrediction.text.text,"
    "suggestions.placePrediction.structuredFormat.mainText.text,"
    "suggestions.placePrediction.structuredFormat.secondaryText.text,"
    "suggestions.placePrediction.distanceMeters"
)

_DETAILS_FIELD_MASK = "id,formattedAddress,location,addressComponents"


def is_google_places_enabled() -> bool:
    return os.getenv("GOOGLE_PLACES_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def is_google_places_auto_fallback() -> bool:
    return os.getenv("GOOGLE_PLACES_AUTO_FALLBACK", "false").strip().lower() in ("1", "true", "yes")


def get_google_places_api_key() -> str:
    return os.getenv("GOOGLE_PLACES_API_KEY", "").strip()


@dataclass
class GooglePlacesPrediction:
    place_id: str
    main_text: str
    secondary_text: str
    full_text: str
    distance_meters: Optional[int] = None


@dataclass
class GoogleAutocompleteOutcome:
    predictions: List[GooglePlacesPrediction]
    http_status: Optional[int] = None
    error: Optional[str] = None
    first_result: Optional[str] = None


@dataclass
class ParsedAddressComponents:
    rua: str = ""
    numero: str = ""
    bairro: str = ""
    cidade: str = ""
    estado: str = ""
    cep: str = ""
    formatted_address: str = ""


def _component_value(components: List[dict], type_name: str) -> str:
    for comp in components:
        types = comp.get("types") or []
        if type_name in types:
            return (comp.get("longText") or comp.get("shortText") or comp.get("text") or "").strip()
    return ""


def _component_short(components: List[dict], type_name: str) -> str:
    for comp in components:
        types = comp.get("types") or []
        if type_name in types:
            return (comp.get("shortText") or comp.get("longText") or comp.get("text") or "").strip()
    return ""


def _parse_google_error(response: requests.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:200] if response.text else f"HTTP {response.status_code}"

    err = data.get("error") or {}
    if isinstance(err, dict):
        status = (err.get("status") or "").strip()
        message = (err.get("message") or "").strip()
        if status and message:
            return f"{status}: {message}"
        if status:
            return status
        if message:
            return message
    if isinstance(data.get("error"), str):
        return data["error"]
    return response.text[:200] if response.text else f"HTTP {response.status_code}"


def parse_address_components(
    components: List[dict],
    formatted_address: str = "",
) -> ParsedAddressComponents:
    rua = _component_value(components, "route")
    if not rua:
        rua = _component_value(components, "street_address")
    numero = _component_value(components, "street_number")
    bairro = (
        _component_value(components, "sublocality_level_1")
        or _component_value(components, "sublocality")
        or _component_value(components, "neighborhood")
        or _component_value(components, "administrative_area_level_3")
    )
    cidade = (
        _component_value(components, "locality")
        or _component_value(components, "administrative_area_level_2")
        or _component_value(components, "postal_town")
    )
    estado = normalize_estado_uf(
        _component_short(components, "administrative_area_level_1"),
        iso3166=None,
    )
    cep = normalize_cep(_component_value(components, "postal_code"))
    return ParsedAddressComponents(
        rua=rua,
        numero=numero,
        bairro=bairro,
        cidade=cidade,
        estado=estado,
        cep=cep,
        formatted_address=formatted_address,
    )


class GooglePlacesClient:
    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = (api_key or get_google_places_api_key()).strip()

    def _headers(self, field_mask: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
            "User-Agent": _USER_AGENT,
        }

    def autocomplete(
        self,
        query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        session_token: Optional[str] = None,
        limit: int = 8,
    ) -> GoogleAutocompleteOutcome:
        if not self.api_key or not (query or "").strip():
            return GoogleAutocompleteOutcome([], None, "missing_api_key_or_query", None)

        body: Dict[str, Any] = {
            "input": query.strip(),
            "languageCode": "pt-BR",
            "regionCode": "BR",
            "includedRegionCodes": ["br"],
        }
        if session_token:
            body["sessionToken"] = session_token

        if latitude is not None and longitude is not None:
            lat = float(latitude)
            lon = float(longitude)
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": 50000.0,
                }
            }
            body["origin"] = {"latitude": lat, "longitude": lon}

        try:
            r = requests.post(
                _AUTOCOMPLETE_URL,
                json=body,
                headers=self._headers(_AUTOCOMPLETE_FIELD_MASK),
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            error = _parse_google_error(e.response) if e.response is not None else str(e)
            logger.warning(
                "GooglePlaces autocomplete HTTP error status=%s error=%s",
                status,
                error,
            )
            return GoogleAutocompleteOutcome([], status, error, None)
        except Exception as e:
            logger.warning("GooglePlaces autocomplete failed: %s", e)
            return GoogleAutocompleteOutcome([], None, str(e), None)

        suggestions = data.get("suggestions") or []
        results: List[GooglePlacesPrediction] = []
        for item in suggestions[:limit]:
            pred = item.get("placePrediction") or {}
            place_id = (pred.get("placeId") or pred.get("place_id") or "").strip()
            if not place_id:
                continue
            structured = pred.get("structuredFormat") or {}
            main_text = (structured.get("mainText") or {}).get("text") or ""
            secondary_text = (structured.get("secondaryText") or {}).get("text") or ""
            full_text = (pred.get("text") or {}).get("text") or f"{main_text}, {secondary_text}".strip(", ")
            dist = pred.get("distanceMeters")
            distance_meters = int(dist) if dist is not None else None
            results.append(
                GooglePlacesPrediction(
                    place_id=place_id,
                    main_text=main_text.strip(),
                    secondary_text=secondary_text.strip(),
                    full_text=full_text.strip(),
                    distance_meters=distance_meters,
                )
            )
        first_result = results[0].full_text if results else None
        return GoogleAutocompleteOutcome(results, 200, None, first_result)

    def get_place_details(
        self,
        place_id: str,
        session_token: Optional[str] = None,
    ) -> Optional[RawAddressHit]:
        if not self.api_key or not (place_id or "").strip():
            return None

        pid = place_id.strip()
        if not pid.startswith("places/"):
            pid = f"places/{pid}"

        params: Dict[str, str] = {}
        if session_token:
            params["sessionToken"] = session_token

        try:
            r = requests.get(
                f"{_PLACES_BASE_URL}/{pid.split('/', 1)[1]}",
                params=params,
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": _DETAILS_FIELD_MASK,
                    "User-Agent": _USER_AGENT,
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("GooglePlaces place details failed place_id=%s: %s", place_id[:20], e)
            return None

        location = data.get("location") or {}
        lat = location.get("latitude")
        lon = location.get("longitude")
        if lat is None or lon is None:
            return None

        formatted = (data.get("formattedAddress") or "").strip()
        components = data.get("addressComponents") or []
        parsed = parse_address_components(components, formatted)

        if not parsed.rua and formatted:
            parsed.rua = formatted.split(",")[0].strip()

        external_id = (data.get("id") or place_id).strip()
        if external_id.startswith("places/"):
            external_id = external_id.split("/", 1)[1]

        return RawAddressHit(
            rua=parsed.rua,
            numero=parsed.numero,
            bairro=parsed.bairro,
            cidade=parsed.cidade,
            estado=parsed.estado,
            cep=parsed.cep,
            latitude=float(lat),
            longitude=float(lon),
            source="google_places",
            external_id=external_id,
        )


def prediction_to_provisional_score(
    prediction: GooglePlacesPrediction,
    query: str,
) -> int:
    score = 30
    q = (query or "").strip().lower()
    main = prediction.main_text.lower()
    if q and main and (q in main or main in q):
        score += 25
    if prediction.distance_meters is not None:
        km = prediction.distance_meters / 1000.0
        if km <= 3:
            score += 40
        elif km <= 10:
            score += 25
        elif km <= 20:
            score += 10
    return score


GOOGLE_AUTO_FALLBACK_MIN_SCORE = int(os.getenv("GOOGLE_AUTO_FALLBACK_MIN_SCORE", "60"))
_session_google_calls: Dict[str, int] = {}


def _session_limit() -> int:
    return int(os.getenv("GOOGLE_PLACES_SESSION_LIMIT", "8"))


def reset_session_google_cost_guard(session_token: Optional[str] = None) -> None:
    if session_token:
        _session_google_calls.pop(session_token, None)
    else:
        _session_google_calls.clear()


def _session_cost_guard_allows(session_token: Optional[str]) -> bool:
    if not session_token:
        return True
    count = _session_google_calls.get(session_token, 0)
    return count < _session_limit()


def _record_session_google_call(session_token: Optional[str]) -> None:
    if not session_token:
        return
    _session_google_calls[session_token] = _session_google_calls.get(session_token, 0) + 1


def _query_has_street_and_number(query: str, hints: Optional[dict] = None) -> bool:
    hints = hints or {}
    rua = (hints.get("rua") or "").strip()
    numero = (hints.get("numero") or "").strip()
    if rua and numero:
        return True
    q = (query or "").strip()
    if not q:
        return False
    nums = re.findall(r"\d+", q)
    if not nums:
        return False
    letters = re.sub(r"\d+", " ", q)
    letters = re.sub(r"[^a-zA-ZÀ-ÿ\s]", " ", letters).strip()
    return len(letters) >= 3


def should_auto_invoke_google_places(
    suggestions: List[dict],
    search_query: str,
    hints: Optional[dict] = None,
    providers_timed_out: bool = False,
    session_token: Optional[str] = None,
) -> Tuple[bool, str, bool]:
    """
    Decide se o backend deve chamar Google Places automaticamente.
    Retorna (invoke, reason, cost_guard_hit).
    """
    if not is_google_places_enabled():
        return False, "google_disabled", False
    if not get_google_places_api_key():
        return False, "google_disabled", False
    if not is_google_places_auto_fallback():
        return False, "auto_fallback_off", False

    if not _session_cost_guard_allows(session_token):
        return False, "session_limit", True

    best_score = max((s.get("score", 0) for s in suggestions), default=0)
    has_street_num = _query_has_street_and_number(search_query, hints)

    if not suggestions:
        return True, "no_results", False
    if providers_timed_out and best_score < GOOGLE_AUTO_FALLBACK_MIN_SCORE:
        return True, "provider_timeout", False
    if best_score < GOOGLE_AUTO_FALLBACK_MIN_SCORE:
        if has_street_num:
            return True, "street_number_weak", False
        return True, "low_score", False
    return False, "score_above_threshold", False


class GooglePlacesProvider(AddressProvider):
    """Mantido por compatibilidade; use GooglePlacesClient para fallback controlado."""

    name = "google_places"

    def search(
        self,
        query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        limit: int = 8,
    ) -> List[RawAddressHit]:
        return []
