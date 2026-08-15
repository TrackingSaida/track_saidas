"""Feature flags e configuração de roteirização."""
from __future__ import annotations

import os
from typing import Literal

OptimizationProviderFlag = Literal["osrm", "google"]
GeometryProviderFlag = Literal["osrm", "google"]
CostObjective = Literal["traveled_hour", "kilometer"]


def get_optimization_provider() -> OptimizationProviderFlag:
    raw = (os.getenv("ROUTING_OPTIMIZATION_PROVIDER") or "osrm").strip().lower()
    return "google" if raw == "google" else "osrm"


def get_geometry_provider() -> GeometryProviderFlag:
    raw = (os.getenv("ROUTING_GEOMETRY_PROVIDER") or "osrm").strip().lower()
    return "google" if raw == "google" else "osrm"


def get_google_cost_objective() -> CostObjective:
    raw = (os.getenv("ROUTING_GOOGLE_COST_OBJECTIVE") or "traveled_hour").strip().lower()
    return "kilometer" if raw == "kilometer" else "traveled_hour"


def get_google_timeout_s() -> float:
    try:
        return float(os.getenv("ROUTING_GOOGLE_TIMEOUT_S") or "30")
    except ValueError:
        return 30.0


def get_google_cloud_project() -> str:
    return (os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or "").strip()
