"""Geometria persistida após optimize, inclusive o trecho até a casa."""
from routing.google_route_optimization import (
    polyline_exceeds_stops_bbox,
    resolve_map_polyline_after_optimize,
)
from routing.polyline_codec import decode_polyline
from routing.types import GeometryResult, RoutingError


def _encode_polyline(coords):
    def _enc_value(curr, prev):
        n = int(round(curr * 1e5)) - int(round(prev * 1e5))
        n = ~(n << 1) if n < 0 else n << 1
        out = ""
        while n >= 0x20:
            out += chr((0x20 | (n & 0x1F)) + 63)
            n >>= 5
        return out + chr(n + 63)

    plat = plng = 0
    encoded = ""
    for lat, lng in coords:
        encoded += _enc_value(lat, plat)
        encoded += _enc_value(lng, plng)
        plat, plng = lat, lng
    return encoded


def test_encode_roundtrip():
    pts = [(-23.55, -46.63), (-23.56, -46.64)]
    encoded = _encode_polyline(pts)
    decoded = decode_polyline(encoded)
    assert len(decoded) == 2
    assert abs(decoded[0][0] - pts[0][0]) < 1e-5
    assert abs(decoded[0][1] - pts[0][1]) < 1e-5


def test_bbox_ok_entre_paradas():
    stops = [(1, -23.55, -46.63), (2, -23.56, -46.64)]
    poly = _encode_polyline([(-23.55, -46.63), (-23.555, -46.635), (-23.56, -46.64)])
    assert polyline_exceeds_stops_bbox(poly, stops) is False


def test_bbox_rejeita_perna_ate_casa():
    stops = [(1, -23.55, -46.63), (2, -23.56, -46.64)]
    poly = _encode_polyline(
        [(-23.55, -46.63), (-23.56, -46.64), (-23.40, -46.90)]
    )
    assert polyline_exceeds_stops_bbox(poly, stops) is True


def test_sem_endpoints_reusa_polyline_do_optimize():
    stops = [(1, -23.55, -46.63), (2, -23.56, -46.64)]
    geom = resolve_map_polyline_after_optimize(
        stops,
        had_vehicle_endpoints=False,
        optimize_polyline="abc",
        optimize_dist_m=100,
        optimize_dur_s=40,
        refresh_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("não deveria refresh")),
    )
    assert geom.ok is True
    assert geom.polyline_encoded == "abc"
    assert geom.distancia_total_m == 100


def test_com_endpoints_mantem_tour_ate_a_casa():
    stops = [(1, -23.55, -46.63), (2, -23.56, -46.64)]

    def fake_refresh(*_a, **_k):
        raise AssertionError("não deveria redesenhar só as paradas")

    geom = resolve_map_polyline_after_optimize(
        stops,
        had_vehicle_endpoints=True,
        optimize_polyline="TOUR_VEICULO",
        optimize_dist_m=1200,
        optimize_dur_s=400,
        refresh_fn=fake_refresh,
    )
    assert geom.ok is True
    assert geom.polyline_encoded == "TOUR_VEICULO"
    assert geom.distancia_total_m == 1200
    assert geom.duracao_total_s == 400


def test_com_endpoints_sem_linha_nao_inventa_trecho():
    stops = [(1, -23.55, -46.63), (2, -23.56, -46.64)]

    geom = resolve_map_polyline_after_optimize(
        stops,
        had_vehicle_endpoints=True,
        optimize_polyline=None,
        refresh_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("não deveria refresh")),
    )
    assert geom.ok is False
    assert geom.polyline_encoded is None
    assert geom.error_code == "ROUTING_NO_POLYLINE"


def test_com_endpoints_linha_ate_a_casa_permanece():
    stops = [(1, -23.55, -46.63), (2, -23.56, -46.64)]
    tour = _encode_polyline([(-23.55, -46.63), (-23.56, -46.64), (-23.40, -46.90)])

    geom = resolve_map_polyline_after_optimize(
        stops,
        had_vehicle_endpoints=True,
        optimize_polyline=tour,
        optimize_dist_m=90000,
        optimize_dur_s=3600,
    )
    assert geom.ok is True
    assert geom.polyline_encoded == tour
    assert geom.distancia_total_m == 90000
