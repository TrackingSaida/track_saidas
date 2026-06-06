"""Testes de priorização suave na otimização de rota."""

from geocode_utils import (
    SOFT_PRIORITY_PENALTY_M,
    nearest_neighbor_order,
    nearest_neighbor_soft_priority,
    otimizar_ordem_entregas,
)

# Coordenadas fictícias em SP (delta ~0.002 graus ≈ 200m)
BASE_LAT = -23.55
BASE_LON = -46.63


def _pt(sid: int, dlat: float = 0, dlon: float = 0):
    return (sid, BASE_LAT + dlat, BASE_LON + dlon)


def test_nearby_flex_before_shopee_with_priority():
    """Flex a 300m e Shopee a 200m: prioridade Flex antecipa Flex."""
    points = [_pt(1, 0, 0), _pt(2, 0.002, 0)]  # Shopee perto, Flex um pouco mais
    penalties = {1: SOFT_PRIORITY_PENALTY_M, 2: 0.0}
    ordered = nearest_neighbor_soft_priority(points, penalties)
    assert ordered[0] == 2


def test_distant_flex_does_not_skip_nearby_shopee():
    """Flex a ~8km não deve vir antes de Shopee a 200m."""
    points = [_pt(1, 0, 0), _pt(2, 0.07, 0)]  # ~7-8 km
    penalties = {1: SOFT_PRIORITY_PENALTY_M, 2: 0.0}
    ordered = nearest_neighbor_soft_priority(points, penalties)
    assert ordered[0] == 1


def test_no_priority_matches_nearest_neighbor():
    points = [_pt(1, 0, 0), _pt(2, 0.01, 0), _pt(3, 0.02, 0)]
    nn = nearest_neighbor_order(points)
    soft = nearest_neighbor_soft_priority(points, {1: 0, 2: 0, 3: 0})
    assert nn == soft


def test_otimizar_with_penalties_returns_priority_soft_mode():
    points = [_pt(10, 0, 0), _pt(20, 0.002, 0)]
    penalties = {10: SOFT_PRIORITY_PENALTY_M, 20: 0.0}
    result = otimizar_ordem_entregas(points, stop_penalties=penalties)
    assert result["modo"] == "priority_soft"
    assert result["ordem"][0] == 20
    assert result["distancia_total_m"] is not None
