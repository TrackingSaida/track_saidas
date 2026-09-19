"""Cliente Google Route Optimization API (optimizeTours / refreshDetailsRoutes).

NÃO faz fallback para OSRM/NN. Erros sobem como RoutingError.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from routing.config import (
    get_google_cloud_project,
    get_google_cost_objective,
    get_google_timeout_s,
)
from routing.hashes import parse_shipment_label, shipment_label
from routing.polyline_codec import decode_polyline
from routing.types import (
    GeometryResult,
    OptimizeRouteResult,
    RoutePoint,
    RoutingError,
    StartPoint,
)

logger = logging.getLogger(__name__)


def _global_time_window() -> Tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < start:
        start = start - timedelta(days=1)
    end = start + timedelta(hours=18)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def build_optimize_tours_body(
    points: Sequence[RoutePoint],
    *,
    start: Optional[StartPoint] = None,
    end: Optional[StartPoint] = None,
    cost_objective: Optional[str] = None,
    populate_polylines: bool = True,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    objective = cost_objective or get_google_cost_objective()
    timeout = timeout_s if timeout_s is not None else get_google_timeout_s()
    global_start, global_end = _global_time_window()

    shipments = []
    labels: List[str] = []
    for sid, lat, lon in points:
        label = shipment_label(sid)
        labels.append(label)
        shipments.append(
            {
                "label": label,
                "deliveries": [
                    {
                        "arrivalWaypoint": {
                            "location": {
                                "latLng": {
                                    "latitude": float(lat),
                                    "longitude": float(lon),
                                }
                            }
                        }
                    }
                ],
            }
        )

    vehicle: Dict[str, Any] = {"label": "motoboy"}
    if start is not None:
        vehicle["startWaypoint"] = {
            "location": {
                "latLng": {
                    "latitude": float(start[0]),
                    "longitude": float(start[1]),
                }
            }
        }
    if end is not None:
        vehicle["endWaypoint"] = {
            "location": {
                "latLng": {
                    "latitude": float(end[0]),
                    "longitude": float(end[1]),
                }
            }
        }
    if objective == "kilometer":
        vehicle["costPerKilometer"] = 1.0
    else:
        vehicle["costPerTraveledHour"] = 1.0

    return {
        "timeout": f"{int(max(1, timeout))}s",
        "considerRoadTraffic": False,
        "populatePolylines": bool(populate_polylines),
        "populateTransitionPolylines": False,
        "model": {
            "shipments": shipments,
            "vehicles": [vehicle],
            "globalStartTime": global_start,
            "globalEndTime": global_end,
        },
        "_meta": {"labels": labels, "cost_objective": objective},
    }


def build_refresh_details_body(
    points_in_order: Sequence[RoutePoint],
    *,
    start: Optional[StartPoint] = None,
    end: Optional[StartPoint] = None,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Monta request com refreshDetailsRoutes (não resolve ordem)."""
    timeout = timeout_s if timeout_s is not None else get_google_timeout_s()
    global_start, global_end = _global_time_window()

    shipments = []
    visits = []
    for idx, (sid, lat, lon) in enumerate(points_in_order):
        label = shipment_label(sid)
        shipments.append(
            {
                "label": label,
                "deliveries": [
                    {
                        "arrivalWaypoint": {
                            "location": {
                                "latLng": {
                                    "latitude": float(lat),
                                    "longitude": float(lon),
                                }
                            }
                        }
                    }
                ],
            }
        )
        visits.append(
            {
                "shipmentIndex": idx,
                "isPickup": False,
                "visitRequestIndex": 0,
            }
        )

    vehicle: Dict[str, Any] = {
        "label": "motoboy",
        "costPerTraveledHour": 1.0,
    }
    if start is not None:
        vehicle["startWaypoint"] = {
            "location": {
                "latLng": {"latitude": float(start[0]), "longitude": float(start[1])}
            }
        }
    if end is not None:
        vehicle["endWaypoint"] = {
            "location": {
                "latLng": {"latitude": float(end[0]), "longitude": float(end[1])}
            }
        }

    route = {
        "vehicleIndex": 0,
        "visits": visits,
        "transitions": [{"travelDuration": "0s"} for _ in range(len(visits) + 1)],
    }

    return {
        "timeout": f"{int(max(1, timeout))}s",
        "populatePolylines": True,
        "populateTransitionPolylines": False,
        "refreshDetailsRoutes": [route],
        "model": {
            "shipments": shipments,
            "vehicles": [vehicle],
            "globalStartTime": global_start,
            "globalEndTime": global_end,
        },
    }


def _extract_metrics(route: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    metrics = route.get("metrics") or {}
    dist = metrics.get("travelDistanceMeters")
    dur = None
    total_dur = metrics.get("totalDuration") or route.get("metrics", {}).get("travelDuration")
    if isinstance(total_dur, str) and total_dur.endswith("s"):
        try:
            dur = int(round(float(total_dur[:-1])))
        except ValueError:
            dur = None
    elif isinstance(total_dur, (int, float)):
        dur = int(round(total_dur))

    if dist is None:
        # soma transitions
        total_m = 0.0
        total_s = 0.0
        for tr in route.get("transitions") or []:
            total_m += float(tr.get("travelDistanceMeters") or 0)
            td = tr.get("travelDuration")
            if isinstance(td, str) and td.endswith("s"):
                try:
                    total_s += float(td[:-1])
                except ValueError:
                    pass
        dist = int(round(total_m)) if total_m else None
        if dur is None and total_s:
            dur = int(round(total_s))
    else:
        dist = int(round(float(dist)))
    return dist, dur


def _shipment_index_from_visit(visit: Dict[str, Any]) -> Optional[int]:
    """Índice 0-based do shipment.

    A Route Optimization REST omite campos proto3 com valor default.
    `shipmentIndex=0` chega como campo ausente, não como inválido.
    """
    raw = visit.get("shipmentIndex")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_optimize_tours_response(
    data: Dict[str, Any],
    *,
    expected_labels: Sequence[str],
) -> OptimizeRouteResult:
    skipped = data.get("skippedShipments") or []
    if skipped:
        raise RoutingError(
            "ROUTING_INCOMPLETE",
            f"Google pulou {len(skipped)} parada(s); rota incompleta.",
            http_status=422,
        )

    routes = data.get("routes") or []
    if not routes:
        raise RoutingError(
            "ROUTING_NO_SOLUTION",
            "Google não retornou rota otimizada.",
            http_status=422,
        )
    route = routes[0]
    visits = route.get("visits") or []
    if len(visits) != len(expected_labels):
        raise RoutingError(
            "ROUTING_VISIT_MISMATCH",
            f"Esperado {len(expected_labels)} visitas, recebido {len(visits)}.",
            http_status=422,
        )

    label_by_index = {i: expected_labels[i] for i in range(len(expected_labels))}
    ordered_ids: List[int] = []
    ordered_labels: List[str] = []
    seen = set()
    for visit in visits:
        idx = _shipment_index_from_visit(visit)
        if idx is None or idx not in label_by_index:
            raise RoutingError(
                "ROUTING_VISIT_MISMATCH",
                "Visita Google com shipmentIndex inválido.",
                http_status=422,
            )
        label = visit.get("shipmentLabel") or label_by_index[idx]
        sid = parse_shipment_label(label)
        if sid is None:
            sid = parse_shipment_label(label_by_index[idx])
        if sid is None:
            raise RoutingError(
                "ROUTING_VISIT_MISMATCH",
                f"Label Google não reconhecido: {label!r}",
                http_status=422,
            )
        if sid in seen:
            raise RoutingError(
                "ROUTING_VISIT_MISMATCH",
                "Visita duplicada na resposta Google.",
                http_status=422,
            )
        seen.add(sid)
        ordered_ids.append(sid)
        ordered_labels.append(label)

    if set(ordered_ids) != {
        parse_shipment_label(lbl) for lbl in expected_labels if parse_shipment_label(lbl) is not None
    }:
        raise RoutingError(
            "ROUTING_VISIT_MISMATCH",
            "Conjunto de paradas da resposta Google diverge do enviado.",
            http_status=422,
        )

    poly = None
    route_poly = route.get("routePolyline") or {}
    if isinstance(route_poly, dict):
        poly = route_poly.get("points") or route_poly.get("encodedPolyline")
    if not poly:
        raise RoutingError(
            "ROUTING_NO_POLYLINE",
            "Resposta Google sem polyline.",
            http_status=422,
        )

    dist_m, dur_s = _extract_metrics(route)
    return OptimizeRouteResult(
        ordem=ordered_ids,
        optimization_mode="google",
        distancia_total_m=dist_m,
        duracao_total_s=dur_s,
        polyline_encoded=str(poly),
        shipment_labels=ordered_labels,
        raw={"provider": "google"},
    )


def parse_refresh_response(data: Dict[str, Any]) -> GeometryResult:
    routes = data.get("routes") or []
    if not routes:
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code="ROUTING_NO_SOLUTION",
        )
    route = routes[0]
    poly = None
    route_poly = route.get("routePolyline") or {}
    if isinstance(route_poly, dict):
        poly = route_poly.get("points") or route_poly.get("encodedPolyline")
    if not poly:
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code="ROUTING_NO_POLYLINE",
        )
    dist_m, dur_s = _extract_metrics(route)
    return GeometryResult(
        polyline_encoded=str(poly),
        distancia_total_m=dist_m,
        duracao_total_s=dur_s,
        geometry_provider="google",
        ok=True,
    )


def _call_optimize_tours_rest(body: Dict[str, Any]) -> Dict[str, Any]:
    project = get_google_cloud_project()
    if not project:
        raise RoutingError(
            "ROUTING_AUTH",
            "GOOGLE_CLOUD_PROJECT não configurado.",
            http_status=503,
        )

    try:
        import google.auth
        import google.auth.transport.requests
        import requests as http_requests
    except ImportError as e:
        raise RoutingError(
            "ROUTING_AUTH",
            f"Dependências Google ausentes: {e}",
            http_status=503,
        ) from e

    # Remove meta interna
    payload = {k: v for k, v in body.items() if not k.startswith("_")}

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        token = credentials.token
    except Exception as e:
        logger.exception("google_auth_failed")
        raise RoutingError(
            "ROUTING_AUTH",
            "Falha de autenticação com Google Cloud.",
            http_status=503,
        ) from e

    url = f"https://routeoptimization.googleapis.com/v1/projects/{project}:optimizeTours"
    timeout = get_google_timeout_s()
    t0 = time.monotonic()
    try:
        resp = http_requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout + 5,
        )
    except Exception as e:
        logger.warning("google_optimize_timeout_or_network err=%s", e)
        raise RoutingError(
            "ROUTING_TIMEOUT",
            "Otimização Google demorou ou falhou na rede.",
            http_status=504,
        ) from e
    finally:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "google_optimize_tours elapsed_ms=%s status=%s shipments=%s",
            elapsed_ms,
            getattr(locals().get("resp", None), "status_code", None),
            len((payload.get("model") or {}).get("shipments") or []),
        )

    if resp.status_code == 429:
        raise RoutingError("ROUTING_QUOTA", "Limite temporário da Route Optimization.", http_status=429)
    if resp.status_code in (401, 403):
        raise RoutingError("ROUTING_AUTH", "Sem permissão na Route Optimization API.", http_status=503)
    if resp.status_code >= 500:
        raise RoutingError("ROUTING_UNAVAILABLE", "Route Optimization indisponível.", http_status=503)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json().get("error", {}).get("message") or "")[:200]
        except Exception:
            detail = (resp.text or "")[:200]
        raise RoutingError(
            "ROUTING_INVALID",
            detail or "Pedido de otimização rejeitado pelo Google.",
            http_status=422,
        )

    try:
        return resp.json()
    except Exception as e:
        raise RoutingError(
            "ROUTING_INVALID",
            "Resposta Google inválida.",
            http_status=502,
        ) from e


def optimize_google(
    points: List[RoutePoint],
    *,
    start: Optional[StartPoint] = None,
    end: Optional[StartPoint] = None,
    populate_polylines: bool = True,
    cost_objective: Optional[str] = None,
) -> OptimizeRouteResult:
    if not points:
        return OptimizeRouteResult(ordem=[], optimization_mode="google")

    body = build_optimize_tours_body(
        points,
        start=start,
        end=end,
        cost_objective=cost_objective,
        populate_polylines=populate_polylines,
    )
    labels = list(body.pop("_meta", {}).get("labels") or [])
    data = _call_optimize_tours_rest(body)
    return parse_optimize_tours_response(data, expected_labels=labels)


def refresh_geometry_google(
    points_in_order: List[RoutePoint],
    *,
    start: Optional[StartPoint] = None,
    end: Optional[StartPoint] = None,
) -> GeometryResult:
    if len(points_in_order) < 1:
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code="ROUTING_EMPTY",
        )
    body = build_refresh_details_body(points_in_order, start=start, end=end)
    data = _call_optimize_tours_rest(body)
    return parse_refresh_response(data)


# ~13 km: pega tour do veículo (Rodoanel etc.) sem recusar desvio urbano típico.
_POLYLINE_BBOX_EXCESS_DEG = 0.12


def polyline_exceeds_stops_bbox(
    encoded: str,
    points: Sequence[RoutePoint],
    *,
    excess_deg: float = _POLYLINE_BBOX_EXCESS_DEG,
) -> bool:
    """True se a polyline sair muito da bbox das paradas (provável perna start/end)."""
    coords = decode_polyline(encoded)
    if not coords or not points:
        return False
    stop_lats = [float(p[1]) for p in points]
    stop_lons = [float(p[2]) for p in points]
    min_lat = min(stop_lats) - excess_deg
    max_lat = max(stop_lats) + excess_deg
    min_lon = min(stop_lons) - excess_deg
    max_lon = max(stop_lons) + excess_deg
    poly_lats = [c[0] for c in coords]
    poly_lons = [c[1] for c in coords]
    return (
        min(poly_lats) < min_lat
        or max(poly_lats) > max_lat
        or min(poly_lons) < min_lon
        or max(poly_lons) > max_lon
    )


def resolve_map_polyline_after_optimize(
    points_in_visit_order: List[RoutePoint],
    *,
    had_vehicle_endpoints: bool,
    optimize_polyline: Optional[str],
    optimize_dist_m: Optional[int] = None,
    optimize_dur_s: Optional[int] = None,
    refresh_fn=None,
) -> GeometryResult:
    """Geometria persistida no mapa: só paradas 1→N.

    `optimizeTours` com start/end devolve o tour do veículo (GPS→paradas→casa).
    Esse blob não deve ir para o mapa; após a ordem, dá refresh sem endpoints.
    """
    refresh = refresh_fn or refresh_geometry_google
    if not had_vehicle_endpoints:
        if optimize_polyline:
            return GeometryResult(
                polyline_encoded=optimize_polyline,
                distancia_total_m=optimize_dist_m,
                duracao_total_s=optimize_dur_s,
                geometry_provider="google",
                ok=True,
            )
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code="ROUTING_NO_POLYLINE",
        )

    try:
        geom = refresh(points_in_visit_order)
    except RoutingError as err:
        logger.warning("refresh stop-to-stop após optimize falhou: %s", err.code)
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code=err.code,
        )

    if not geom.ok or not geom.polyline_encoded:
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code=geom.error_code or "ROUTING_NO_POLYLINE",
        )

    if polyline_exceeds_stops_bbox(geom.polyline_encoded, points_in_visit_order):
        logger.warning("polyline Google expandiu bbox das paradas; descartada")
        return GeometryResult(
            polyline_encoded=None,
            distancia_total_m=None,
            duracao_total_s=None,
            geometry_provider="google",
            ok=False,
            error_code="ROUTING_POLYLINE_BBOX",
        )
    return geom
