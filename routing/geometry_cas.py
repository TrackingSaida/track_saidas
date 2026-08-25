"""CAS de geometria: evita sobrescrever polyline com resposta atrasada."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from routing.hashes import order_hash

if TYPE_CHECKING:
    from models import RotasMotoboy

logger = logging.getLogger(__name__)


def parse_ordem_json(raw) -> List[int]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw]
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [int(x) for x in data]


def capture_geometry_cas_token(rota: "RotasMotoboy") -> tuple:
    """Retorna (expected_route_revision, expected_geometry_order_hash)."""
    revision = int(getattr(rota, "route_revision", 0) or 0)
    ordem = parse_ordem_json(rota.ordem_json)
    return revision, order_hash(ordem)


def try_persist_geometry_cas(
    db: Session,
    *,
    rota_id: int,
    expected_route_revision: int,
    expected_geometry_order_hash: str,
    polyline_encoded: str,
    geometry_provider: str,
    distancia_total_m: Optional[int] = None,
    duracao_total_s: Optional[int] = None,
) -> bool:
    """
    Persiste polyline somente se revision + hash da ordem ainda batem.
    Retorna True se aplicou; False se descartou (race / resposta tardia).
    """
    from models import RotasMotoboy

    rota = db.get(RotasMotoboy, rota_id)
    if rota is None:
        logger.warning("geometry_cas_rejected reason=rota_missing rota_id=%s", rota_id)
        return False

    current_revision = int(getattr(rota, "route_revision", 0) or 0)
    current_hash = order_hash(parse_ordem_json(rota.ordem_json))

    if current_revision != int(expected_route_revision):
        logger.info(
            "geometry_cas_rejected reason=revision_mismatch rota_id=%s expected=%s actual=%s",
            rota_id,
            expected_route_revision,
            current_revision,
        )
        return False
    if current_hash != expected_geometry_order_hash:
        logger.info(
            "geometry_cas_rejected reason=order_hash_mismatch rota_id=%s",
            rota_id,
        )
        return False

    # UPDATE condicional fecha race na escrita
    updated = (
        db.query(RotasMotoboy)
        .filter(
            RotasMotoboy.id == rota_id,
            RotasMotoboy.route_revision == int(expected_route_revision),
        )
        .update(
            {
                RotasMotoboy.polyline_encoded: polyline_encoded,
                RotasMotoboy.geometry_provider: geometry_provider,
                RotasMotoboy.geometry_status: "valid",
                RotasMotoboy.geometry_order_hash: expected_geometry_order_hash,
                RotasMotoboy.distancia_total_m: distancia_total_m,
                RotasMotoboy.duracao_total_s: duracao_total_s,
                RotasMotoboy.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        logger.info(
            "geometry_cas_rejected reason=update_race rota_id=%s revision=%s",
            rota_id,
            expected_route_revision,
        )
        return False
    return True


def mark_geometry_stale(db: Session, rota: "RotasMotoboy") -> None:
    rota.geometry_status = "stale"
    rota.updated_at = datetime.utcnow()


def bump_route_revision(rota: "RotasMotoboy", ordem: Sequence[int]) -> int:
    """Incrementa revision, marca geometria stale, atualiza ordem."""
    current = int(getattr(rota, "route_revision", 0) or 0)
    rota.route_revision = current + 1
    rota.ordem_json = json.dumps([int(x) for x in ordem])
    rota.geometry_status = "stale"
    rota.updated_at = datetime.utcnow()
    return int(rota.route_revision)


def geometry_payload_for_api(rota: Optional["RotasMotoboy"]) -> dict:
    """Só serve polyline se status=valid e hash confere com ordem atual."""
    if rota is None:
        return {
            "geometry_status": "missing",
            "geometry_provider": None,
            "route_revision": 0,
            "polyline_encoded": None,
            "polyline_coords": None,
        }

    status = getattr(rota, "geometry_status", None) or "missing"
    revision = int(getattr(rota, "route_revision", 0) or 0)
    provider = getattr(rota, "geometry_provider", None)
    encoded = getattr(rota, "polyline_encoded", None)
    stored_hash = getattr(rota, "geometry_order_hash", None)
    current_hash = order_hash(parse_ordem_json(rota.ordem_json))

    serve = (
        status == "valid"
        and bool(encoded)
        and stored_hash is not None
        and stored_hash == current_hash
    )
    coords = None
    if serve:
        from routing.polyline_codec import polyline_to_coords_dicts

        coords = polyline_to_coords_dicts(encoded)

    return {
        "geometry_status": status if serve or status != "valid" else "stale",
        "geometry_provider": provider,
        "route_revision": revision,
        "polyline_encoded": encoded if serve else None,
        "polyline_coords": coords if serve else None,
        "distancia_total_m": getattr(rota, "distancia_total_m", None) if serve else None,
        "duracao_total_s": getattr(rota, "duracao_total_s", None) if serve else None,
    }
