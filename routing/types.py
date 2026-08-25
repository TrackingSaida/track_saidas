"""Tipos internos de roteirização."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

RoutePoint = Tuple[int, float, float]  # (id_saida/representative_id, lat, lon)
StartPoint = Tuple[float, float]

OptimizationMode = Literal["google", "osrm", "priority_soft", "nearest_fallback"]
GeometryProvider = Literal["google", "osrm"]
GeometryStatus = Literal["valid", "stale", "missing", "failed"]


@dataclass
class OptimizeRouteResult:
    ordem: List[int]
    optimization_mode: OptimizationMode
    distancia_total_m: Optional[int] = None
    duracao_total_s: Optional[int] = None
    polyline_encoded: Optional[str] = None
    shipment_labels: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GeometryResult:
    polyline_encoded: Optional[str]
    distancia_total_m: Optional[int]
    duracao_total_s: Optional[int]
    geometry_provider: GeometryProvider
    ok: bool = True
    error_code: Optional[str] = None


class RoutingError(Exception):
    """Erro de negócio/provider de roteirização (sem fallback silencioso)."""

    def __init__(self, code: str, message: str, *, http_status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
