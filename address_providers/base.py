"""Interface de providers de endereço."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


def provider_http_timeout_sec() -> float:
    return float(os.getenv("ADDRESS_PROVIDER_TIMEOUT_SEC", "2")) + 1.0


@dataclass
class RawAddressHit:
    rua: str
    numero: str
    bairro: str
    cidade: str
    estado: str
    cep: str
    latitude: float
    longitude: float
    source: str
    external_id: str = field(default="")


class AddressProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(
        self,
        query: str,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        limit: int = 8,
    ) -> List[RawAddressHit]:
        raise NotImplementedError
