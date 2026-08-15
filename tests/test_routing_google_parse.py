"""Testes do parser Google e ausência de fallback silencioso."""
import os
from unittest.mock import patch

import pytest

from routing.google_route_optimization import (
    build_optimize_tours_body,
    parse_optimize_tours_response,
)
from routing.hashes import shipment_label
from routing.service import optimize_route_order
from routing.types import RoutingError


def test_build_optimize_uses_traveled_hour_by_default(monkeypatch):
    monkeypatch.setenv("ROUTING_GOOGLE_COST_OBJECTIVE", "traveled_hour")
    body = build_optimize_tours_body([(1, -23.5, -46.8), (2, -23.6, -46.7)])
    vehicle = body["model"]["vehicles"][0]
    assert vehicle.get("costPerTraveledHour") == 1.0
    assert "costPerKilometer" not in vehicle
    assert body["populatePolylines"] is True
    assert body["considerRoadTraffic"] is False


def test_build_optimize_kilometer(monkeypatch):
    monkeypatch.setenv("ROUTING_GOOGLE_COST_OBJECTIVE", "kilometer")
    body = build_optimize_tours_body([(1, -23.5, -46.8)])
    vehicle = body["model"]["vehicles"][0]
    assert vehicle.get("costPerKilometer") == 1.0
    assert "costPerTraveledHour" not in vehicle


def test_parse_optimize_ok():
    labels = [shipment_label(10), shipment_label(20)]
    data = {
        "routes": [
            {
                "visits": [
                    {"shipmentIndex": 1, "shipmentLabel": labels[1]},
                    {"shipmentIndex": 0, "shipmentLabel": labels[0]},
                ],
                "routePolyline": {"points": "encodedpoly"},
                "metrics": {"travelDistanceMeters": 1234, "totalDuration": "120s"},
                "transitions": [],
            }
        ],
        "skippedShipments": [],
    }
    result = parse_optimize_tours_response(data, expected_labels=labels)
    assert result.ordem == [20, 10]
    assert result.optimization_mode == "google"
    assert result.polyline_encoded == "encodedpoly"
    assert result.distancia_total_m == 1234


def test_parse_skipped_raises():
    labels = [shipment_label(1)]
    with pytest.raises(RoutingError) as ei:
        parse_optimize_tours_response(
            {"routes": [{"visits": [], "routePolyline": {"points": "x"}}], "skippedShipments": [{}]},
            expected_labels=labels,
        )
    assert ei.value.code == "ROUTING_INCOMPLETE"


def test_parse_missing_polyline_raises():
    labels = [shipment_label(1)]
    with pytest.raises(RoutingError) as ei:
        parse_optimize_tours_response(
            {
                "routes": [{"visits": [{"shipmentIndex": 0}], "routePolyline": {}}],
                "skippedShipments": [],
            },
            expected_labels=labels,
        )
    assert ei.value.code == "ROUTING_NO_POLYLINE"


def test_google_provider_error_does_not_fallback_to_osrm(monkeypatch):
    monkeypatch.setenv("ROUTING_OPTIMIZATION_PROVIDER", "google")
    points = [(1, -23.5, -46.8), (2, -23.51, -46.81)]

    def boom(*_a, **_k):
        raise RoutingError("ROUTING_TIMEOUT", "timeout", http_status=504)

    with patch("routing.google_route_optimization.optimize_google", side_effect=boom):
        with patch("routing.osrm_provider.optimize_osrm") as osrm_mock:
            with pytest.raises(RoutingError) as ei:
                optimize_route_order(points)
            assert ei.value.code == "ROUTING_TIMEOUT"
            osrm_mock.assert_not_called()


def test_priority_soft_uses_local_not_google(monkeypatch):
    monkeypatch.setenv("ROUTING_OPTIMIZATION_PROVIDER", "google")
    points = [(1, -23.5, -46.8), (2, -23.51, -46.81)]
    with patch("routing.google_route_optimization.optimize_google") as g_mock:
        result = optimize_route_order(points, stop_penalties={1: 0.0, 2: 500.0})
        assert result.optimization_mode == "priority_soft"
        g_mock.assert_not_called()
