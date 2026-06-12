"""
Agrupamento de entregas em paradas para otimização OSRM.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def normalize_street(text: str) -> str:
    return " ".join(_strip_accents((text or "").strip().lower()).split())


def normalize_numero(numero: str, endereco: str = "") -> str:
    n = (numero or "").strip()
    if n:
        digits = re.sub(r"\D", "", n)
        return digits or n.lower()
    m = re.search(r",?\s*(\d{1,6})\s*(?:,|$)", endereco or "")
    return m.group(1) if m else ""


def normalize_cep(cep: str) -> str:
    digits = re.sub(r"\D", "", cep or "")
    return digits[:8] if len(digits) >= 8 else digits


def normalize_cidade(text: str) -> str:
    return normalize_street(text)


def build_stop_key(detail: Any, id_saida: int) -> str:
    """Espelha prioridades mobile: CEP+num → rua+num+cidade → coord → id."""
    cep = normalize_cep(getattr(detail, "dest_cep", None) or "")
    numero = normalize_numero(
        getattr(detail, "dest_numero", None) or "",
        getattr(detail, "dest_rua", None) or "",
    )
    rua = normalize_street(getattr(detail, "dest_rua", None) or "")
    cidade = normalize_cidade(getattr(detail, "dest_cidade", None) or "")

    if cep and numero:
        return f"cep|{cep}|{numero}"
    if rua and numero and cidade:
        return f"loc|{rua}|{numero}|{cidade}"
    if rua and numero:
        return f"loc|{rua}|{numero}|"

    lat = getattr(detail, "latitude", None)
    lon = getattr(detail, "longitude", None)
    if lat is not None and lon is not None:
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            if math.isfinite(lat_f) and math.isfinite(lon_f) and not (lat_f == 0 and lon_f == 0):
                return f"coord|{round(lat_f, 5)}|{round(lon_f, 5)}"
        except (TypeError, ValueError):
            pass

    addr = (getattr(detail, "endereco_formatado", None) or "").strip()
    if addr:
        return f"addr|{normalize_street(addr)}"
    return f"id|{id_saida}"


@dataclass
class RouteStop:
    stop_key: str
    representative_id: int
    delivery_ids: List[int] = field(default_factory=list)
    lat: Optional[float] = None
    lon: Optional[float] = None
    address_label: str = ""

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None


def _address_label(detail: Any) -> str:
    if detail is None:
        return ""
    fmt = (getattr(detail, "endereco_formatado", None) or "").strip()
    if fmt:
        return fmt
    parts = [
        getattr(detail, "dest_rua", None),
        getattr(detail, "dest_numero", None),
        getattr(detail, "dest_bairro", None),
    ]
    return ", ".join(p for p in parts if p and str(p).strip())


def build_route_stops(
    delivery_ids: Sequence[int],
    details_map: Dict[int, Any],
) -> List[RouteStop]:
    """Agrupa entregas consecutivas na ordem de entrada por stop_key."""
    groups: List[RouteStop] = []
    key_to_index: Dict[str, int] = {}

    for sid in delivery_ids:
        sid_int = int(sid)
        detail = details_map.get(sid_int)
        key = build_stop_key(detail, sid_int)

        lat: Optional[float] = None
        lon: Optional[float] = None
        if detail is not None:
            try:
                if detail.latitude is not None and detail.longitude is not None:
                    lat = float(detail.latitude)
                    lon = float(detail.longitude)
                    if not math.isfinite(lat) or not math.isfinite(lon):
                        lat, lon = None, None
                    elif lat == 0 and lon == 0:
                        lat, lon = None, None
            except (TypeError, ValueError):
                lat, lon = None, None

        if key in key_to_index:
            idx = key_to_index[key]
            groups[idx].delivery_ids.append(sid_int)
            if groups[idx].lat is None and lat is not None:
                groups[idx].lat = lat
                groups[idx].lon = lon
        else:
            stop = RouteStop(
                stop_key=key,
                representative_id=sid_int,
                delivery_ids=[sid_int],
                lat=lat,
                lon=lon,
                address_label=_address_label(detail),
            )
            key_to_index[key] = len(groups)
            groups.append(stop)

    # representative_id = primeiro com coords ou primeiro da lista
    for stop in groups:
        rep = stop.delivery_ids[0]
        for did in stop.delivery_ids:
            d = details_map.get(did)
            if d is not None and d.latitude is not None and d.longitude is not None:
                rep = did
                break
        stop.representative_id = rep

    return groups


def expand_stop_order(optimized_stop_ids: Sequence[int], stops: List[RouteStop]) -> List[int]:
    """Expande ordem OSRM (representative ids) para todos os delivery_ids da parada."""
    rep_to_stop: Dict[int, RouteStop] = {}
    for stop in stops:
        rep_to_stop[stop.representative_id] = stop
        for did in stop.delivery_ids:
            if did not in rep_to_stop:
                rep_to_stop[did] = stop

    result: List[int] = []
    seen_stops: set[str] = set()

    for rep_id in optimized_stop_ids:
        stop = rep_to_stop.get(int(rep_id))
        if stop is None:
            result.append(int(rep_id))
            continue
        if stop.stop_key in seen_stops:
            continue
        seen_stops.add(stop.stop_key)
        result.extend(stop.delivery_ids)

    return result
