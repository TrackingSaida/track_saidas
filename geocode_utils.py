"""
Geocoding helpers para salvar latitude/longitude no backend.

Camada principal:
- geocode_address_any: tenta provedor externo configurável (ex.: Geoapify ou LocationIQ)
  e, em seguida, faz fallback para Nominatim (OpenStreetMap).
"""
from __future__ import annotations

import logging
import math
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import requests

from geocode_cache import get_cached, set_cached

logger = logging.getLogger(__name__)


def _infer_nominatim_precision(
    first: dict,
    expected_numero: Optional[str] = None,
) -> str:
    addr = first.get("address") or {}
    house = str(addr.get("house_number") or "").strip()
    exp_num = normalize_address_text(expected_numero or "")
    house_norm = normalize_address_text(house)

    if expected_numero and expected_numero.strip():
        if house and (
            house_norm == exp_num
            or exp_num in house_norm
            or house_norm in exp_num
        ):
            return "rooftop"
        if house:
            return "street"
        logger.info(
            "Geocoding (OSM): sem house_number para numero esperado %s",
            expected_numero,
        )
        return "approx"

    if house:
        return "rooftop"

    typ = str(first.get("type") or "").lower()
    if typ in ("house", "building", "residential"):
        return "rooftop"
    if typ in ("road", "street", "pedestrian"):
        return "street"
    return "approx"


def geocode_address(
    address: str,
    expected_numero: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    """
    Geocoding via Nominatim (OpenStreetMap).

    Retorna (lat, lon, precision) com precision em rooftop|street|approx.
    """
    if not (address or "").strip():
        return None
    addr = address.strip()
    query = addr if addr.endswith("Brasil") else f"{addr}, Brasil"
    url = "https://nominatim.openstreetmap.org/search"
    try:
        r = requests.get(
            url,
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "TrackSaidasApp/1.0 (https://github.com/track-saidas; contato@track-saidas.com)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            logger.warning("Geocoding (OSM): nenhum resultado para %s", query[:80])
            return None
        first = data[0]
        lat = first.get("lat") or first.get("latitude")
        lon = first.get("lon") or first.get("longitude")
        if lat is not None and lon is not None:
            precision = _infer_nominatim_precision(first, expected_numero)
            logger.info(
                "Geocoding (OSM) ok: %s -> %s, %s precision=%s",
                query[:50],
                lat,
                lon,
                precision,
            )
            return (float(lat), float(lon), precision)
        return None
    except Exception as e:
        logger.warning("Geocoding (OSM) falhou para %s: %s", query[:80], e)
        return None


def _geocode_with_google(
    address: str,
    api_key: str,
    expected_numero: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    try:
        r = requests.get(
            url,
            params={"address": address, "key": api_key, "region": "br"},
            headers={"User-Agent": "TrackSaidasBackend/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or data.get("status") != "OK":
            logger.warning(
                "Geocoding (google): status=%s para %s",
                data.get("status") if isinstance(data, dict) else None,
                address[:80],
            )
            return None
        results = data.get("results") or []
        if not results:
            return None
        first = results[0]
        location = (first.get("geometry") or {}).get("location") or {}
        lat = location.get("lat")
        lon = location.get("lng")
        if lat is None or lon is None:
            return None
        location_type = str((first.get("geometry") or {}).get("location_type") or "").upper()
        precision_map = {
            "ROOFTOP": "rooftop",
            "RANGE_INTERPOLATED": "rooftop",
            "GEOMETRIC_CENTER": "street",
            "APPROXIMATE": "approx",
        }
        precision = precision_map.get(location_type, "approx")
        if expected_numero and expected_numero.strip() and precision != "approx":
            exp_digits = re.sub(r"\D", "", expected_numero)
            formatted = str(first.get("formatted_address") or "")
            fmt_digits = re.sub(r"\D", "", formatted)
            if exp_digits and exp_digits not in fmt_digits:
                precision = "approx"
        logger.info(
            "Geocoding (google) ok: %s -> %s, %s precision=%s",
            address[:50],
            lat,
            lon,
            precision,
        )
        return float(lat), float(lon), precision
    except Exception as e:
        logger.warning("Geocoding (google) falhou para %s: %s", address[:80], e)
        return None


def _external_provider_precision(expected_numero: Optional[str]) -> str:
    return "street" if (expected_numero or "").strip() else "approx"


def _geocode_with_geoapify(
    address: str,
    api_key: str,
    expected_numero: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    url = "https://api.geoapify.com/v1/geocode/search"
    try:
        r = requests.get(
            url,
            params={"text": address, "format": "json", "limit": 1, "apiKey": api_key},
            headers={"User-Agent": "TrackSaidasBackend/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results") if isinstance(data, dict) else data
        if not results:
            logger.warning("Geocoding (geoapify): nenhum resultado para %s", address[:80])
            return None
        first = results[0]
        lat = first.get("lat") or first.get("latitude")
        lon = first.get("lon") or first.get("longitude")
        if lat is not None and lon is not None:
            precision = _external_provider_precision(expected_numero)
            logger.info("Geocoding (geoapify) ok: %s -> %s, %s precision=%s", address[:50], lat, lon, precision)
            return float(lat), float(lon), precision
        return None
    except Exception as e:
        logger.warning("Geocoding (geoapify) falhou para %s: %s", address[:80], e)
        return None


def _geocode_with_locationiq(
    address: str,
    api_key: str,
    expected_numero: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    url = "https://us1.locationiq.com/v1/search.php"
    try:
        r = requests.get(
            url,
            params={"key": api_key, "q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "TrackSaidasBackend/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            logger.warning("Geocoding (locationiq): nenhum resultado para %s", address[:80])
            return None
        first = data[0]
        lat = first.get("lat") or first.get("latitude")
        lon = first.get("lon") or first.get("longitude")
        if lat is not None and lon is not None:
            precision = _external_provider_precision(expected_numero)
            logger.info("Geocoding (locationiq) ok: %s -> %s, %s precision=%s", address[:50], lat, lon, precision)
            return float(lat), float(lon), precision
        return None
    except Exception as e:
        logger.warning("Geocoding (locationiq) falhou para %s: %s", address[:80], e)
        return None


def _geocode_with_maps_co(
    address: str,
    api_key: str,
    expected_numero: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    """Geocoding via geocode.maps.co (OSM/Nominatim, plano gratuito com chave)."""
    url = "https://geocode.maps.co/search"
    try:
        r = requests.get(
            url,
            params={"q": address, "format": "json", "limit": 1, "api_key": api_key},
            headers={"User-Agent": "TrackSaidasBackend/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            logger.warning("Geocoding (maps.co): nenhum resultado para %s", address[:80])
            return None
        first = data[0]
        lat = first.get("lat") or first.get("latitude")
        lon = first.get("lon") or first.get("longitude")
        if lat is not None and lon is not None:
            precision = _external_provider_precision(expected_numero)
            logger.info("Geocoding (maps.co) ok: %s -> %s, %s precision=%s", address[:50], lat, lon, precision)
            return float(lat), float(lon), precision
        return None
    except Exception as e:
        logger.warning("Geocoding (maps.co) falhou para %s: %s", address[:80], e)
        return None


def normalize_cep(cep: Optional[str]) -> str:
    digits = re.sub(r"\D", "", cep or "")
    return digits[:8] if len(digits) >= 8 else digits


def normalize_address_text(text: Optional[str]) -> str:
    raw = (text or "").strip().lower()
    raw = unicodedata.normalize("NFD", raw)
    raw = "".join(c for c in raw if unicodedata.category(c) != "Mn")
    raw = re.sub(r"\bn[º°o]\b", "numero", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()


def build_geocode_queries(
    rua: Optional[str] = None,
    numero: Optional[str] = None,
    complemento: Optional[str] = None,
    bairro: Optional[str] = None,
    cidade: Optional[str] = None,
    estado: Optional[str] = None,
    cep: Optional[str] = None,
    endereco_formatado: Optional[str] = None,
) -> List[str]:
    """Queries priorizadas: CEP+número primeiro, depois fallbacks sem duplicar."""
    rua_n = (rua or "").strip()
    num_n = (numero or "").strip()
    bairro_n = (bairro or "").strip()
    cidade_n = (cidade or "").strip()
    estado_n = (estado or "").strip()
    cep_n = normalize_cep(cep)
    comp_n = (complemento or "").strip()
    fmt = (endereco_formatado or "").strip()

    queries: List[str] = []
    seen: set[str] = set()

    def _add(*parts: Optional[str], suffix: str = "Brasil") -> None:
        q = ", ".join(p for p in parts if p and str(p).strip()).strip()
        if not q:
            return
        if suffix and not q.endswith(suffix):
            q = f"{q}, {suffix}"
        norm = normalize_address_text(q)
        if norm and norm not in seen:
            seen.add(norm)
            queries.append(q)

    if cep_n and num_n:
        _add(f"{cep_n} {num_n}")
    if cep_n and rua_n and num_n and cidade_n:
        _add(cep_n, f"{rua_n} {num_n}", f"{cidade_n} {estado_n}".strip())
    if rua_n and num_n:
        _add(rua_n, num_n, bairro_n, f"{cidade_n} {estado_n}".strip(), cep_n or None)
    if rua_n and num_n and comp_n:
        _add(rua_n, num_n, comp_n, bairro_n, f"{cidade_n} {estado_n}".strip(), cep_n or None)
    if fmt:
        _add(fmt)
    if rua_n and bairro_n and cidade_n:
        _add(rua_n, bairro_n, f"{cidade_n} {estado_n}".strip())
    if rua_n and cidade_n:
        _add(rua_n, cidade_n, estado_n)
    if bairro_n and cidade_n:
        _add(bairro_n, cidade_n, estado_n)
    if cidade_n:
        _add(cidade_n, estado_n)

    return queries


def geocode_address_any(
    address: str,
    db: Optional[Any] = None,
    expected_numero: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    """
    Wrapper de alto nível: tenta provedor externo configurável e faz fallback para OSM.

    Configuração via ambiente:
    - GEOCODER_PROVIDER: \"geoapify\" | \"locationiq\" | \"geocode_maps_co\" (default: \"geoapify\")
    - GEOCODER_API_KEY: chave do provedor externo (obrigatória para geoapify/locationiq/maps_co)
    """
    addr = (address or "").strip()
    if not addr:
        return None

    cached = get_cached(db, addr)
    if cached:
        precision = _external_provider_precision(expected_numero)
        return cached[0], cached[1], precision

    google_enabled = os.getenv("GOOGLE_GEOCODING_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    google_key = (
        os.getenv("GOOGLE_GEOCODING_API_KEY", "").strip()
        or os.getenv("GEOCODER_API_KEY", "").strip()
    )
    if google_enabled and google_key:
        google_coords = _geocode_with_google(addr, google_key, expected_numero)
        if google_coords:
            set_cached(db, addr, google_coords[0], google_coords[1], "google")
            logger.info("geocode_attempt query=%s provider=google success=true", addr[:80])
            return google_coords

    provider = os.getenv("GEOCODER_PROVIDER", "geoapify").strip().lower()
    provider_used: Optional[str] = None
    api_key = os.getenv("GEOCODER_API_KEY", "").strip()

    # 1) Tenta provedor externo, se configurado
    if api_key:
        if provider in ("geoapify", "geo"):
            coords = _geocode_with_geoapify(addr, api_key, expected_numero)
            if coords:
                provider_used = "geoapify"
                set_cached(db, addr, coords[0], coords[1], provider_used)
                logger.info("geocode_attempt query=%s provider=%s success=true", addr[:80], provider_used)
                return coords
        elif provider in ("locationiq", "lq"):
            coords = _geocode_with_locationiq(addr, api_key, expected_numero)
            if coords:
                provider_used = "locationiq"
                set_cached(db, addr, coords[0], coords[1], provider_used)
                logger.info("geocode_attempt query=%s provider=%s success=true", addr[:80], provider_used)
                return coords
        elif provider in ("geocode_maps_co", "maps_co", "mapsco"):
            coords = _geocode_with_maps_co(addr, api_key, expected_numero)
            if coords:
                provider_used = "maps_co"
                set_cached(db, addr, coords[0], coords[1], provider_used)
                logger.info("geocode_attempt query=%s provider=%s success=true", addr[:80], provider_used)
                return coords
        else:
            logger.warning("GEOCODER_PROVIDER '%s' desconhecido; usando apenas OSM", provider)

    # 2) Fallback para Nominatim/OpenStreetMap
    coords = geocode_address(addr, expected_numero=expected_numero)
    if coords:
        set_cached(db, addr, coords[0], coords[1], "nominatim")
        logger.info("geocode_attempt query=%s provider=nominatim success=true", addr[:80])
    else:
        logger.warning("geocode_attempt query=%s provider=any success=false", addr[:80])
    return coords


def _nominatim_city_name(addr: dict) -> str:
    return (
        str(addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or addr.get("county") or "")
        .strip()
    )


def _nominatim_state_uf(addr: dict) -> str:
    iso = str(addr.get("ISO3166-2-lvl4") or "").strip().upper()
    if iso.startswith("BR-") and len(iso) >= 5:
        return iso[3:5]
    state = str(addr.get("state") or "").strip()
    state_norm = normalize_address_text(state)
    uf_map = {
        "sao paulo": "SP",
        "rio de janeiro": "RJ",
        "minas gerais": "MG",
    }
    if len(state) == 2:
        return state.upper()
    return uf_map.get(state_norm, state[:2].upper() if len(state) >= 2 else "")


def _country_is_brazil(addr: dict) -> bool:
    country = normalize_address_text(str(addr.get("country") or ""))
    code = str(addr.get("country_code") or "").strip().lower()
    return code == "br" or country in ("brasil", "brazil")


def _cities_match(expected_cidade: str, addr: dict) -> bool:
    expected = normalize_address_text(expected_cidade)
    city = normalize_address_text(_nominatim_city_name(addr))
    if not expected or not city:
        return False
    return expected == city or expected in city or city in expected


def _states_match(expected_estado: str, addr: dict) -> bool:
    exp = normalize_address_text(expected_estado)
    if len(exp) == 2:
        return _nominatim_state_uf(addr) == exp.upper()
    state = normalize_address_text(str(addr.get("state") or ""))
    return bool(state and (exp in state or state in exp))


def _cep_prefix_matches(expected_cep: str, addr: dict) -> bool:
    exp = normalize_cep(expected_cep)
    if len(exp) < 5:
        return True
    got = normalize_cep(str(addr.get("postcode") or ""))
    if len(got) < 5:
        return True
    return exp[:5] == got[:5]


def validate_nominatim_candidate(
    candidate: dict,
    *,
    cidade: str,
    estado: str,
    cep: Optional[str] = None,
) -> bool:
    addr = candidate.get("address") or {}
    if not isinstance(addr, dict):
        return False
    if not _country_is_brazil(addr):
        return False
    if not _states_match(estado, addr):
        return False
    if not _cities_match(cidade, addr):
        return False
    if not _cep_prefix_matches(cep or "", addr):
        return False
    lat = candidate.get("lat") or candidate.get("latitude")
    lon = candidate.get("lon") or candidate.get("longitude")
    if lat is None or lon is None:
        return False
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return False
    if lat_f == 0 and lon_f == 0:
        return False
    return True


def geocode_address_strict(
    *,
    rua: Optional[str] = None,
    numero: Optional[str] = None,
    bairro: Optional[str] = None,
    cidade: Optional[str] = None,
    estado: Optional[str] = None,
    cep: Optional[str] = None,
    db: Optional[Any] = None,
) -> Optional[Tuple[float, float, str, str, float]]:
    """
    Geocode com validação de cidade/estado/CEP. Retorna
    (lat, lon, precision, source, score) ou None.
    """
    cidade_n = (cidade or "").strip()
    estado_n = (estado or "").strip()
    rua_n = (rua or "").strip()
    if not cidade_n or not estado_n or len(rua_n) < 3:
        return None

    parts = [
        rua_n,
        f"número {numero}" if (numero or "").strip() else None,
        (bairro or "").strip() or None,
        cidade_n,
        estado_n,
        normalize_cep(cep) or None,
        "Brasil",
    ]
    query = ", ".join(p for p in parts if p)
    url = "https://nominatim.openstreetmap.org/search"
    try:
        r = requests.get(
            url,
            params={
                "q": query,
                "format": "json",
                "addressdetails": 1,
                "limit": 5,
                "dedupe": 1,
                "countrycodes": "br",
            },
            headers={"User-Agent": "TrackSaidasApp/1.0 (https://github.com/track-saidas; contato@track-saidas.com)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return None
        for item in data:
            if not validate_nominatim_candidate(
                item, cidade=cidade_n, estado=estado_n, cep=cep
            ):
                continue
            lat = float(item["lat"])
            lon = float(item["lon"])
            addr = item.get("address") or {}
            house = str(addr.get("house_number") or "").strip()
            precision = "rooftop" if house else "street"
            score = 90.0 if precision == "rooftop" else 70.0
            set_cached(db, query, lat, lon, "nominatim_strict")
            return lat, lon, precision, "nominatim_strict", score
    except Exception as e:
        logger.warning("geocode_address_strict falhou: %s", e)
    return None


def geocode_address_with_fallbacks(
    rua: Optional[str] = None,
    numero: Optional[str] = None,
    complemento: Optional[str] = None,
    bairro: Optional[str] = None,
    cidade: Optional[str] = None,
    estado: Optional[str] = None,
    cep: Optional[str] = None,
    endereco_formatado: Optional[str] = None,
    db: Optional[Any] = None,
) -> Optional[Tuple[float, float, str]]:
    """
    Tenta geocoding com queries priorizadas (CEP+número primeiro).
    Retorna (lat, lon, precision).
    """
    queries = build_geocode_queries(
        rua=rua,
        numero=numero,
        complemento=complemento,
        bairro=bairro,
        cidade=cidade,
        estado=estado,
        cep=cep,
        endereco_formatado=endereco_formatado,
    )
    for query in queries:
        result = geocode_address_any(query, db=db, expected_numero=numero)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Otimização de rota (OSRM Trip + nearest neighbor / haversine)
# ---------------------------------------------------------------------------

OSRM_TRIP_BASE = "https://router.project-osrm.org/trip/v1/driving"
OSRM_HTTP_TIMEOUT = 10

RoutePoint = Tuple[int, float, float]  # id_saida, latitude, longitude
StartPoint = Tuple[float, float]  # latitude, longitude


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em metros entre dois pontos (fórmula de Haversine)."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


SOFT_PRIORITY_THRESHOLD_M = 1500.0
SOFT_PRIORITY_PENALTY_M = 500.0


def nearest_neighbor_soft_priority(
    points: List[RoutePoint],
    stop_penalties: Dict[int, float],
    start: Optional[StartPoint] = None,
    threshold_m: float = SOFT_PRIORITY_THRESHOLD_M,
) -> List[int]:
    """
    Vizinho mais próximo com penalidade suave por parada.
    Penalidade só aplica quando distância < threshold_m.
    """
    if not points:
        return []
    if len(points) == 1:
        return [points[0][0]]

    remaining = list(points)
    ordered_ids: List[int] = []

    if start is not None:
        cur_lat, cur_lon = start[0], start[1]
    else:
        first = remaining.pop(0)
        ordered_ids.append(first[0])
        cur_lat, cur_lon = first[1], first[2]

    while remaining:
        best_idx = 0
        best_cost = float("inf")
        for i, (sid, lat, lon) in enumerate(remaining):
            dist_m = haversine_m(cur_lat, cur_lon, lat, lon)
            penalty = stop_penalties.get(sid, 0.0)
            cost = dist_m if dist_m >= threshold_m else dist_m + penalty
            if cost < best_cost:
                best_cost = cost
                best_idx = i
        next_pt = remaining.pop(best_idx)
        ordered_ids.append(next_pt[0])
        cur_lat, cur_lon = next_pt[1], next_pt[2]

    return ordered_ids


def nearest_neighbor_order(
    points: List[RoutePoint],
    start: Optional[StartPoint] = None,
) -> List[int]:
    """
    Ordena entregas por vizinho mais próximo (Haversine).
    points: lista (id_saida, lat, lon) na ordem original de referência.
    """
    if not points:
        return []
    if len(points) == 1:
        return [points[0][0]]

    remaining = list(points)
    ordered_ids: List[int] = []

    if start is not None:
        cur_lat, cur_lon = start[0], start[1]
    else:
        first = remaining.pop(0)
        ordered_ids.append(first[0])
        cur_lat, cur_lon = first[1], first[2]

    while remaining:
        best_idx = 0
        best_dist = float("inf")
        for i, (_, lat, lon) in enumerate(remaining):
            dist = haversine_m(cur_lat, cur_lon, lat, lon)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        next_pt = remaining.pop(best_idx)
        ordered_ids.append(next_pt[0])
        cur_lat, cur_lon = next_pt[1], next_pt[2]

    return ordered_ids


def _otimizar_ordem_osrm_trip(
    points: List[RoutePoint],
    start: Optional[StartPoint] = None,
) -> Optional[Tuple[List[int], int, int]]:
    """
    Otimiza ordem via OSRM Trip. Retorna (ids_ordenados, distancia_m, duracao_s) ou None.
    Se start for informado, é o primeiro waypoint (não é entrega).
    """
    if len(points) < 1:
        return None
    if len(points) == 1 and start is None:
        return None

    id_by_input_index: List[Optional[int]] = []
    coord_pairs: List[Tuple[float, float]] = []

    if start is not None:
        id_by_input_index.append(None)
        coord_pairs.append((start[0], start[1]))

    for sid, lat, lon in points:
        id_by_input_index.append(sid)
        coord_pairs.append((lat, lon))

    if len(coord_pairs) < 2:
        return None

    coords_str = ";".join(f"{lon},{lat}" for lat, lon in coord_pairs)
    url = f"{OSRM_TRIP_BASE}/{coords_str}"
    params = {
        "roundtrip": "false",
        "overview": "false",
        "source": "first",
        "destination": "any",
    }

    try:
        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": "TrackSaidasBackend/1.0"},
            timeout=OSRM_HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("OSRM Trip falhou (rede/timeout): %s", e)
        return None

    if not isinstance(data, dict) or data.get("code") != "Ok":
        logger.warning(
            "OSRM Trip resposta inválida: code=%s",
            data.get("code") if isinstance(data, dict) else None,
        )
        return None

    trips = data.get("trips") or []
    waypoints = data.get("waypoints") or []
    if not trips or len(waypoints) != len(id_by_input_index):
        logger.warning("OSRM Trip: trips/waypoints ausentes ou tamanho inconsistente")
        return None

    trip0 = trips[0]
    distance_m = int(round(float(trip0.get("distance") or 0)))
    duration_s = int(round(float(trip0.get("duration") or 0)))

    delivery_entries: List[Tuple[int, int]] = []
    for i, wp in enumerate(waypoints):
        if i == 0 and start is not None:
            continue
        sid = id_by_input_index[i]
        if sid is None:
            continue
        widx = wp.get("waypoint_index")
        if widx is None:
            logger.warning("OSRM Trip: waypoint_index ausente no índice %s", i)
            return None
        delivery_entries.append((int(widx), sid))

    delivery_entries.sort(key=lambda x: x[0])
    ordered_ids = [sid for _, sid in delivery_entries]
    if len(ordered_ids) != len(points):
        logger.warning("OSRM Trip: ordem retornada incompleta")
        return None

    return ordered_ids, distance_m, duration_s


def _route_distance_duration_m(points: List[RoutePoint], ordered_ids: List[int]) -> Tuple[int, int]:
    """Distância e duração estimadas (30 km/h) para ordem de paradas."""
    id_to_coord = {sid: (lat, lon) for sid, lat, lon in points}
    dist_m = 0.0
    prev: Optional[Tuple[float, float]] = None
    for sid in ordered_ids:
        coord = id_to_coord.get(sid)
        if coord is None:
            continue
        if prev is not None:
            dist_m += haversine_m(prev[0], prev[1], coord[0], coord[1])
        prev = coord
    duration_s = int(round((dist_m / 1000.0 / 30.0) * 3600)) if dist_m > 0 else 0
    return int(round(dist_m)), duration_s


def otimizar_ordem_entregas(
    points: List[RoutePoint],
    start: Optional[StartPoint] = None,
    stop_penalties: Optional[Dict[int, float]] = None,
) -> Dict[str, Any]:
    """
    Tenta OSRM Trip; em falha usa nearest neighbor.
    Com stop_penalties, usa nearest_neighbor_soft_priority (modo priority_soft).
    Retorna dict com ordem, modo, distancia_total_m, duracao_total_s.
    """
    if not points:
        return {
            "ordem": [],
            "modo": "nearest_fallback",
            "distancia_total_m": None,
            "duracao_total_s": None,
        }

    if stop_penalties:
        ordered = nearest_neighbor_soft_priority(points, stop_penalties, start=start)
        dist_m, dur_s = _route_distance_duration_m(points, ordered)
        return {
            "ordem": ordered,
            "modo": "priority_soft",
            "distancia_total_m": dist_m,
            "duracao_total_s": dur_s,
        }

    if len(points) >= 2 or (len(points) == 1 and start is not None):
        osrm_result = _otimizar_ordem_osrm_trip(points, start=start)
        if osrm_result is not None:
            ordered, dist_m, dur_s = osrm_result
            return {
                "ordem": ordered,
                "modo": "osrm_trip",
                "distancia_total_m": dist_m,
                "duracao_total_s": dur_s,
            }

    ordered = nearest_neighbor_order(points, start=start)
    dist_m, dur_s = _route_distance_duration_m(points, ordered)
    return {
        "ordem": ordered,
        "modo": "nearest_fallback",
        "distancia_total_m": dist_m,
        "duracao_total_s": dur_s,
    }
