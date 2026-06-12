"""Provider Nominatim (proxy backend)."""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from address_providers.base import AddressProvider, RawAddressHit, provider_http_timeout_sec
from address_normalizer import normalize_cep, normalize_estado_uf

logger = logging.getLogger(__name__)

_USER_AGENT = "TrackSaidasApp/1.0 (https://github.com/track-saidas; contato@track-saidas.com)"


class NominatimProvider(AddressProvider):
    name = "nominatim"

    def search(
        self,
        query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        limit: int = 8,
    ) -> List[RawAddressHit]:
        q = (query or "").strip()
        if len(q) < 3:
            return []
        params = {
            "q": q if q.endswith("Brasil") else f"{q}, Brasil",
            "format": "json",
            "addressdetails": 1,
            "limit": limit,
            "countrycodes": "br",
        }
        if latitude is not None and longitude is not None:
            params["viewbox"] = f"{longitude - 0.3},{latitude + 0.3},{longitude + 0.3},{latitude - 0.3}"
            params["bounded"] = 0
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params,
                headers={"User-Agent": _USER_AGENT},
                timeout=provider_http_timeout_sec(),
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            hits: List[RawAddressHit] = []
            for item in data:
                hit = self._map_item(item)
                if hit:
                    hits.append(hit)
            return hits
        except Exception as e:
            logger.warning("NominatimProvider search failed: %s", e)
            return []

    def _map_item(self, item: dict) -> Optional[RawAddressHit]:
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            return None
        addr = item.get("address") or {}
        rua = (addr.get("road") or addr.get("pedestrian") or addr.get("street") or "").strip()
        numero = str(addr.get("house_number") or "").strip()
        bairro = (
            addr.get("suburb")
            or addr.get("neighbourhood")
            or addr.get("quarter")
            or addr.get("city_district")
            or ""
        ).strip()
        cidade = (
            addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
        ).strip()
        estado = normalize_estado_uf(
            addr.get("state"),
            iso3166=addr.get("ISO3166-2-lvl4"),
        )
        cep = normalize_cep(addr.get("postcode") or "")
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
            external_id=str(item.get("place_id") or ""),
        )
