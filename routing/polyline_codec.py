"""Decode de encoded polyline (Google Encoded Polyline Algorithm)."""
from __future__ import annotations

from typing import List, Tuple


def decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    """Retorna lista de (lat, lon)."""
    if not encoded:
        return []
    coords: List[Tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)

    while index < length:
        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += dlat

        shift = 0
        result = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if (result & 1) else (result >> 1)
        lng += dlng

        coords.append((lat / 1e5, lng / 1e5))

    return coords


def polyline_to_coords_dicts(encoded: Optional[str]) -> List[dict]:
    if not encoded:
        return []
    return [
        {"latitude": lat, "longitude": lon} for lat, lon in decode_polyline(encoded)
    ]
