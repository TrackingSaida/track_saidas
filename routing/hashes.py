"""Hashes canônicos para idempotência e CAS de geometria."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple


def quantize_coord(value: float, decimals: int = 4) -> float:
    return round(float(value), decimals)


def quantize_point(
    lat: Optional[float], lon: Optional[float], decimals: int = 4
) -> Optional[Dict[str, float]]:
    if lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    return {
        "latitude": quantize_coord(lat_f, decimals),
        "longitude": quantize_coord(lon_f, decimals),
    }


def canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def order_hash(ordem: Sequence[int]) -> str:
    """Hash estável da sequência de id_saida (CAS de geometria)."""
    ids = [int(x) for x in ordem]
    return sha256_hex({"ordem": ids})


def optimization_input_hash(
    *,
    optimization_provider: str,
    geometry_provider: str,
    delivery_ids: Sequence[int],
    stop_representative_ids: Sequence[int],
    start: Optional[Tuple[float, float]],
    end: Optional[Tuple[float, float]],
    priority: Optional[Dict[str, Any]],
    cost_objective: str,
) -> str:
    start_q = quantize_point(start[0], start[1]) if start else None
    end_q = quantize_point(end[0], end[1]) if end else None
    return sha256_hex(
        {
            "optimization_provider": optimization_provider,
            "geometry_provider": geometry_provider,
            "delivery_ids": sorted({int(i) for i in delivery_ids}),
            "stop_representative_ids": [int(i) for i in stop_representative_ids],
            "start": start_q,
            "end": end_q,
            "priority": priority,
            "cost_objective": cost_objective,
        }
    )


def shipment_label(representative_id: int) -> str:
    return f"stop:repr={int(representative_id)}"


def parse_shipment_label(label: str) -> Optional[int]:
    prefix = "stop:repr="
    if not label or not label.startswith(prefix):
        return None
    try:
        return int(label[len(prefix) :])
    except ValueError:
        return None
