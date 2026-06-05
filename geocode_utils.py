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
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Geocoding via Nominatim (OpenStreetMap).

    Mantida como fallback quando o provedor externo falhar ou não estiver configurado.
    """
    if not (address or "").strip():
        return None
    addr = address.strip()
    query = addr if addr.endswith("Brasil") else f"{addr}, Brasil"
    url = "https://nominatim.openstreetmap.org/search"
    try:
        r = requests.get(
            url,
            params={"q": query, "format": "json", "limit": 1},
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
            logger.info("Geocoding (OSM) ok: %s -> %s, %s", query[:50], lat, lon)
            return (float(lat), float(lon))
        return None
    except Exception as e:
        logger.warning("Geocoding (OSM) falhou para %s: %s", query[:80], e)
        return None


def _geocode_with_geoapify(address: str, api_key: str) -> Optional[Tuple[float, float]]:
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
            logger.info("Geocoding (geoapify) ok: %s -> %s, %s", address[:50], lat, lon)
            return float(lat), float(lon)
        return None
    except Exception as e:
        logger.warning("Geocoding (geoapify) falhou para %s: %s", address[:80], e)
        return None


def _geocode_with_locationiq(address: str, api_key: str) -> Optional[Tuple[float, float]]:
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
            logger.info("Geocoding (locationiq) ok: %s -> %s, %s", address[:50], lat, lon)
            return float(lat), float(lon)
        return None
    except Exception as e:
        logger.warning("Geocoding (locationiq) falhou para %s: %s", address[:80], e)
        return None


def _geocode_with_maps_co(address: str, api_key: str) -> Optional[Tuple[float, float]]:
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
            logger.info("Geocoding (maps.co) ok: %s -> %s, %s", address[:50], lat, lon)
            return float(lat), float(lon)
        return None
    except Exception as e:
        logger.warning("Geocoding (maps.co) falhou para %s: %s", address[:80], e)
        return None


def geocode_address_any(address: str) -> Optional[Tuple[float, float]]:
    """
    Wrapper de alto nível: tenta provedor externo configurável e faz fallback para OSM.

    Configuração via ambiente:
    - GEOCODER_PROVIDER: \"geoapify\" | \"locationiq\" | \"geocode_maps_co\" (default: \"geoapify\")
    - GEOCODER_API_KEY: chave do provedor externo (obrigatória para geoapify/locationiq/maps_co)
    """
    addr = (address or "").strip()
    if not addr:
        return None

    provider = os.getenv("GEOCODER_PROVIDER", "geoapify").strip().lower()
    api_key = os.getenv("GEOCODER_API_KEY", "").strip()

    # 1) Tenta provedor externo, se configurado
    if api_key:
        if provider in ("geoapify", "geo"):
            coords = _geocode_with_geoapify(addr, api_key)
            if coords:
                return coords
        elif provider in ("locationiq", "lq"):
            coords = _geocode_with_locationiq(addr, api_key)
            if coords:
                return coords
        elif provider in ("geocode_maps_co", "maps_co", "mapsco"):
            coords = _geocode_with_maps_co(addr, api_key)
            if coords:
                return coords
        else:
            logger.warning("GEOCODER_PROVIDER '%s' desconhecido; usando apenas OSM", provider)

    # 2) Fallback para Nominatim/OpenStreetMap
    return geocode_address(addr)


def geocode_address_with_fallbacks(
    rua: Optional[str] = None,
    numero: Optional[str] = None,
    complemento: Optional[str] = None,
    bairro: Optional[str] = None,
    cidade: Optional[str] = None,
    estado: Optional[str] = None,
    cep: Optional[str] = None,
    endereco_formatado: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    """
    Tenta geocoding com várias variações da query para maximizar chance de obter coordenadas.
    Ordem: endereço completo → sem complemento → sem número → cidade + estado.
    """
    def _try(*parts: Optional[str]) -> Optional[Tuple[float, float]]:
        query = ", ".join(p for p in parts if p and str(p).strip()).strip()
        if not query:
            return None
        if not query.endswith("Brasil"):
            query = f"{query}, Brasil"
        return geocode_address_any(query)

    # 1) Endereço formatado completo (como veio do app)
    if endereco_formatado and endereco_formatado.strip():
        coords = _try(endereco_formatado.strip())
        if coords:
            return coords

    # 2) Partes: rua, número, bairro, cidade, estado
    coords = _try(rua, numero, bairro, cidade, estado)
    if coords:
        return coords

    # 3) Sem número (às vezes o número atrapalha)
    coords = _try(rua, bairro, cidade, estado)
    if coords:
        return coords

    # 4) Rua, cidade, estado
    coords = _try(rua, cidade, estado)
    if coords:
        return coords

    # 5) Bairro, cidade, estado
    coords = _try(bairro, cidade, estado)
    if coords:
        return coords

    # 6) Cidade, estado (centro da cidade como último recurso)
    coords = _try(cidade, estado)
    if coords:
        return coords

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


def otimizar_ordem_entregas(
    points: List[RoutePoint],
    start: Optional[StartPoint] = None,
) -> Dict[str, Any]:
    """
    Tenta OSRM Trip; em falha usa nearest neighbor.
    Retorna dict com ordem, modo, distancia_total_m, duracao_total_s.
    """
    if not points:
        return {
            "ordem": [],
            "modo": "nearest_fallback",
            "distancia_total_m": None,
            "duracao_total_s": None,
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
    return {
        "ordem": ordered,
        "modo": "nearest_fallback",
        "distancia_total_m": None,
        "duracao_total_s": None,
    }
