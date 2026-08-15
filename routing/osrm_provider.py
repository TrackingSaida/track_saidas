"""Provider OSRM — ordem via Trip (com NN fallback legado somente neste path)."""
from __future__ import annotations

from typing import Dict, List, Optional

from geocode_utils import otimizar_ordem_entregas
from routing.types import OptimizeRouteResult, RoutePoint, StartPoint


def optimize_osrm(
    points: List[RoutePoint],
    *,
    start: Optional[StartPoint] = None,
    end: Optional[StartPoint] = None,
    stop_penalties: Optional[Dict[int, float]] = None,
) -> OptimizeRouteResult:
    """
    Usa a lógica legada.
    Com stop_penalties → priority_soft (deliberado).
    Sem penalties → OSRM Trip; se falhar → nearest_fallback (só neste provider).
    """
    result = otimizar_ordem_entregas(
        points, start=start, end=end, stop_penalties=stop_penalties
    )
    modo = str(result.get("modo") or "nearest_fallback")
    if modo == "osrm_trip":
        mode = "osrm"
    elif modo == "priority_soft":
        mode = "priority_soft"
    else:
        mode = "nearest_fallback"
    return OptimizeRouteResult(
        ordem=list(result.get("ordem") or []),
        optimization_mode=mode,  # type: ignore[arg-type]
        distancia_total_m=result.get("distancia_total_m"),
        duracao_total_s=result.get("duracao_total_s"),
        raw={"modo_legacy": modo},
    )
