"""CAS de geometria — resposta atrasada não sobrescreve."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from routing.geometry_cas import try_persist_geometry_cas
from routing.hashes import order_hash as oh


def test_cas_discards_stale_revision_response():
    """
    refresh revision 6 inicia
    → rota muda para revision 7
    → resposta revision 6 chega
    → descartada; geometry da rev 7 preservada
    """
    rota_rev7 = SimpleNamespace(
        id=99,
        route_revision=7,
        ordem_json="[1,2,3]",
        polyline_encoded="poly_rev7",
        geometry_status="valid",
        geometry_order_hash=oh([1, 2, 3]),
    )

    db = MagicMock()
    db.get.return_value = rota_rev7
    db.query.return_value.filter.return_value.update.return_value = 0

    expected_rev = 6
    expected_hash = oh([1, 2, 3])

    applied = try_persist_geometry_cas(
        db,
        rota_id=99,
        expected_route_revision=expected_rev,
        expected_geometry_order_hash=expected_hash,
        polyline_encoded="poly_rev6_late",
        geometry_provider="google",
        distancia_total_m=100,
        duracao_total_s=50,
    )
    assert applied is False
    db.query.return_value.filter.return_value.update.assert_not_called()
    assert rota_rev7.polyline_encoded == "poly_rev7"


def test_cas_applies_when_revision_matches():
    rota = SimpleNamespace(
        id=1,
        route_revision=6,
        ordem_json="[10,20]",
        polyline_encoded=None,
        geometry_status="stale",
        geometry_order_hash=None,
    )
    db = MagicMock()
    db.get.return_value = rota
    db.query.return_value.filter.return_value.update.return_value = 1

    expected_hash = oh([10, 20])
    applied = try_persist_geometry_cas(
        db,
        rota_id=1,
        expected_route_revision=6,
        expected_geometry_order_hash=expected_hash,
        polyline_encoded="newpoly",
        geometry_provider="google",
    )
    assert applied is True
    db.query.return_value.filter.return_value.update.assert_called_once()
