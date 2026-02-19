"""
Geocoding via Nominatim (OpenStreetMap) para obter lat/long a partir do endereço.
Usado ao salvar endereço no backend quando o cliente não envia latitude/longitude.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Retorna (latitude, longitude) ou None se não encontrar.
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
            logger.warning("Geocoding: nenhum resultado para %s", query[:80])
            return None
        first = data[0]
        lat = first.get("lat")
        lon = first.get("lon")
        if lat is not None and lon is not None:
            logger.info("Geocoding ok: %s -> %s, %s", query[:50], lat, lon)
            return (float(lat), float(lon))
        return None
    except Exception as e:
        logger.warning("Geocoding falhou para %s: %s", query[:80], e)
        return None
