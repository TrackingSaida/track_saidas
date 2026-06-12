"""Provider Geoapify para busca de endereços."""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import requests

from address_providers.base import AddressProvider, RawAddressHit, provider_http_timeout_sec
from address_normalizer import normalize_cep, normalize_estado_uf

logger = logging.getLogger(__name__)


class GeoapifyProvider(AddressProvider):
    name = "geoapify"

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("GEOCODER_API_KEY", "")

    def search(
        self,
        query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        limit: int = 8,
    ) -> List[RawAddressHit]:
        if not self.api_key or not (query or "").strip():
            return []
        url = "https://api.geoapify.com/v1/geocode/search"
        params = {
            "text": query.strip(),
            "format": "json",
            "limit": limit,
            "apiKey": self.api_key,
            "filter": "countrycode:br",
        }
        if latitude is not None and longitude is not None:
            params["bias"] = f"proximity:{longitude},{latitude}"
        try:
            r = requests.get(
                url,
                params=params,
                headers={"User-Agent": "TrackSaidasBackend/1.0"},
                timeout=provider_http_timeout_sec(),
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("results") if isinstance(data, dict) else data
            if not isinstance(results, list):
                return []
            hits: List[RawAddressHit] = []
            for item in results:
                hit = self._map_item(item)
                if hit:
                    hits.append(hit)
            return hits
        except Exception as e:
            logger.warning("GeoapifyProvider search failed: %s", e)
            return []

    def _map_item(self, item: dict) -> Optional[RawAddressHit]:
        lat = item.get("lat") or item.get("latitude")
        lon = item.get("lon") or item.get("longitude")
        if lat is None or lon is None:
            return None
        props = item if "street" in item else item.get("properties", item)
        rua = (props.get("street") or props.get("road") or props.get("name") or "").strip()
        numero = str(props.get("housenumber") or props.get("house_number") or "").strip()
        bairro = (props.get("suburb") or props.get("neighbourhood") or props.get("district") or "").strip()
        cidade = (props.get("city") or props.get("town") or props.get("village") or props.get("municipality") or "").strip()
        state_code = (props.get("state_code") or "").strip().upper()
        if len(state_code) == 2 and state_code.isascii() and state_code.isalpha():
            estado = state_code
        else:
            estado = normalize_estado_uf(props.get("state"))
        cep = normalize_cep(props.get("postcode") or props.get("postal_code") or "")
        if not rua and not cidade:
            return None
        return RawAddressHit(
            rua=rua,
            numero=numero,
            bairro=bairro,
            cidade=cidade,
            estado=estado,
            cep=cep,
            latitude=float(lat),
            longitude=float(lon),
            source=self.name,
            external_id=str(item.get("place_id") or item.get("id") or ""),
        )
