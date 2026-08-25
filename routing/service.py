"""Orquestração de otimização: priority / osrm / google (sem fallback Google→OSRM)."""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from routing.config import get_geometry_provider, get_google_cost_objective, get_optimization_provider
from routing.osrm_provider import optimize_osrm
from routing.types import OptimizeRouteResult, RoutePoint, RoutingError, StartPoint

logger = logging.getLogger(__name__)


def optimize_route_order(
    points: List[RoutePoint],
    *,
    start: Optional[StartPoint] = None,
    end: Optional[StartPoint] = None,
    stop_penalties: Optional[Dict[int, float]] = None,
    populate_polylines: Optional[bool] = None,
) -> OptimizeRouteResult:
    """
    Ramifica:
    - (a) stop_penalties → priority_soft (deliberado, local)
    - (b) ROUTING_OPTIMIZATION_PROVIDER=osrm → OSRM (+ NN fallback legado)
    - (c) ROUTING_OPTIMIZATION_PROVIDER=google → só Google; erro propaga
    """
    if stop_penalties:
        result = optimize_osrm(
            points, start=start, end=end, stop_penalties=stop_penalties
        )
        # Garante mode priority_soft
        result.optimization_mode = "priority_soft"
        # Sem polyline no soft local; geometria virá de refresh se geometry_provider=google
        result.polyline_encoded = None
        return result

    provider = get_optimization_provider()
    if provider == "google":
        from routing.google_route_optimization import optimize_google

        want_poly = (
            populate_polylines
            if populate_polylines is not None
            else (get_geometry_provider() == "google")
        )
        logger.info(
            "routing_optimize provider=google points=%s cost=%s polylines=%s",
            len(points),
            get_google_cost_objective(),
            want_poly,
        )
        return optimize_google(
            points,
            start=start,
            end=end,
            populate_polylines=want_poly,
            cost_objective=get_google_cost_objective(),
        )

    logger.info("routing_optimize provider=osrm points=%s", len(points))
    return optimize_osrm(points, start=start, end=end, stop_penalties=None)


def map_routing_error_to_http(err: RoutingError) -> tuple:
    return err.http_status, {"code": err.code, "message": err.message}
