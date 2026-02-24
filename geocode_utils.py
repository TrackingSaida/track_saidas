"""
Geocoding helpers para salvar latitude/longitude no backend.

Camada principal:
- geocode_address_any: tenta provedor externo configurável (ex.: Geoapify ou LocationIQ)
  e, em seguida, faz fallback para Nominatim (OpenStreetMap).
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Geocoding via Nominatim (OpenStreetMap).

    Mantida como fallback quando o provedor externo falhar ou não estiver configurado.
    """
    if not (address or "").strip():
        return None
    query = f"{address.strip()}, Brasil"
    url = "https://nominatim.openstreetmap.org/search"
    try:
        r = requests.get(
            url,
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "TrackSaidasBackend/1.0 (contato@seudominio.com)"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            logger.warning("Geocoding (OSM): nenhum resultado para %s", query[:80])
            return None
        first = data[0]
        lat = first.get("lat")
        lon = first.get("lon")
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
        lat = first.get("lat")
        lon = first.get("lon")
        if lat is not None and lon is not None:
            logger.info("Geocoding (locationiq) ok: %s -> %s, %s", address[:50], lat, lon)
            return float(lat), float(lon)
        return None
    except Exception as e:
        logger.warning("Geocoding (locationiq) falhou para %s: %s", address[:80], e)
        return None


def geocode_address_any(address: str) -> Optional[Tuple[float, float]]:
    """
    Wrapper de alto nível: tenta provedor externo configurável e faz fallback para OSM.

    Configuração via ambiente:
    - GEOCODER_PROVIDER: \"geoapify\" | \"locationiq\" (default: \"geoapify\")
    - GEOCODER_API_KEY: chave do provedor externo (obrigatória para os dois)
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
        else:
            logger.warning("GEOCODER_PROVIDER '%s' desconhecido; usando apenas OSM", provider)

    # 2) Fallback para Nominatim/OpenStreetMap
    return geocode_address(addr)
