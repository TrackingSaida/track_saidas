"""Stub para Google Places — implementação futura."""
from __future__ import annotations

from typing import List, Optional

from address_providers.base import AddressProvider, RawAddressHit


class GooglePlacesProvider(AddressProvider):
    name = "google_places"

    def search(
        self,
        query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        limit: int = 8,
    ) -> List[RawAddressHit]:
        return []
