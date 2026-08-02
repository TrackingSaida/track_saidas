"""Testes de priorização suave e destino final na otimização de rota."""

from geocode_utils import (
    SOFT_PRIORITY_PENALTY_M,
    nearest_neighbor_order,
    nearest_neighbor_soft_priority,
    otimizar_ordem_entregas,
)

# Coordenadas fictícias em SP (delta ~0.001 graus ≈ 111m)
BASE_LAT = -23.55
BASE_LON = -46.63
START = (BASE_LAT, BASE_LON)


def _pt(sid: int, dlat: float = 0, dlon: float = 0):
    return (sid, BASE_LAT + dlat, BASE_LON + dlon)


def test_both_nearby_picks_closest_regardless_of_priority():
    """Shopee a ~200m e Flex a ~300m (ambos nearby): ganha o mais perto."""
    points = [_pt(1, 0.0018, 0), _pt(2, 0.0027, 0)]
    penalties = {1: SOFT_PRIORITY_PENALTY_M, 2: 0.0}
    ordered = nearest_neighbor_soft_priority(points, penalties, start=START)
    assert ordered[0] == 1


def test_nearby_non_priority_before_farther_priority():
    """Shopee a ~200m e Flex prioritário a ~600m: Shopee primeiro (nearby sem penalidade)."""
    points = [_pt(1, 0.0018, 0), _pt(2, 0.0054, 0)]
    penalties = {1: SOFT_PRIORITY_PENALTY_M, 2: 0.0}
    ordered = nearest_neighbor_soft_priority(points, penalties, start=START)
    assert ordered[0] == 1


def test_priority_helps_outside_nearby_comparable():
    """Fora do nearby: Flex a ~500m (prio) vs Shopee a ~650m → Flex."""
    points = [_pt(1, 0.00585, 0), _pt(2, 0.0045, 0)]  # ~650m, ~500m
    penalties = {1: SOFT_PRIORITY_PENALTY_M, 2: 0.0}
    ordered = nearest_neighbor_soft_priority(points, penalties, start=START)
    assert ordered[0] == 2


def test_distant_flex_does_not_skip_nearby_shopee():
    """Flex a ~8km não deve vir antes de Shopee a ~200m."""
    points = [_pt(1, 0.0018, 0), _pt(2, 0.07, 0)]
    penalties = {1: SOFT_PRIORITY_PENALTY_M, 2: 0.0}
    ordered = nearest_neighbor_soft_priority(points, penalties, start=START)
    assert ordered[0] == 1


def test_no_priority_matches_nearest_neighbor():
    points = [_pt(1, 0, 0), _pt(2, 0.01, 0), _pt(3, 0.02, 0)]
    nn = nearest_neighbor_order(points)
    soft = nearest_neighbor_soft_priority(points, {1: 0, 2: 0, 3: 0})
    assert nn == soft


def test_otimizar_with_penalties_returns_priority_soft_mode():
    points = [_pt(10, 0.0018, 0), _pt(20, 0.0054, 0)]
    penalties = {10: SOFT_PRIORITY_PENALTY_M, 20: 0.0}
    result = otimizar_ordem_entregas(points, start=START, stop_penalties=penalties)
    assert result["modo"] == "priority_soft"
    assert result["ordem"][0] == 10
    assert result["distancia_total_m"] is not None


def test_nearest_neighbor_with_end_prefers_stop_near_home_last():
    """Com destino ao sul, a última parada tende a ficar perto do fim."""
    # A ao norte, B ao sul (perto do end), C no meio-sul
    points = [
        _pt(1, 0.01, 0),   # norte
        _pt(2, -0.01, 0),  # sul
        _pt(3, -0.005, 0), # meio-sul
    ]
    end = (BASE_LAT - 0.02, BASE_LON)
    ordered = nearest_neighbor_order(points, start=START, end=end)
    assert ordered[-1] == 2
    assert 1 in ordered and 3 in ordered


def test_otimizar_with_end_fallback_includes_end_in_distance():
    points = [_pt(1, 0.01, 0), _pt(2, -0.01, 0)]
    end = (BASE_LAT - 0.02, BASE_LON)
    # Força fallback NN (sem chamar OSRM): usa stop_penalties vazios? better mock.
    # Sem penalties tenta OSRM; com penalties usa soft. Usar NN direto via penalties neutras.
    result = otimizar_ordem_entregas(
        points,
        start=START,
        end=end,
        stop_penalties={1: 0.0, 2: 0.0},
    )
    assert result["modo"] == "priority_soft"
    assert set(result["ordem"]) == {1, 2}
    assert result["ordem"][-1] == 2
    # Distância deve incluir trecho final até end (> só entre paradas)
    without_end = otimizar_ordem_entregas(
        points,
        start=START,
        stop_penalties={1: 0.0, 2: 0.0},
    )
    assert result["distancia_total_m"] > without_end["distancia_total_m"]
