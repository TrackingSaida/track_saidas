"""Camada de roteirização (ordem + geometria) com providers OSRM / Google."""

from routing.config import (
    get_geometry_provider,
    get_google_cost_objective,
    get_optimization_provider,
)
from routing.types import (
    GeometryProvider,
    GeometryStatus,
    OptimizationMode,
    OptimizeRouteResult,
    RoutePoint,
    StartPoint,
)

__all__ = [
    "GeometryProvider",
    "GeometryStatus",
    "OptimizationMode",
    "OptimizeRouteResult",
    "RoutePoint",
    "StartPoint",
    "get_geometry_provider",
    "get_google_cost_objective",
    "get_optimization_provider",
]
