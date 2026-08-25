"""Testes de hashes canônicos e quantização GPS."""
from routing.hashes import optimization_input_hash, order_hash, quantize_point, shipment_label


def test_quantize_point_4_decimals():
    a = quantize_point(-23.54123456, -46.84119876)
    b = quantize_point(-23.54121111, -46.84115555)
    assert a == b  # mesma célula ~11m


def test_optimization_input_hash_stable_with_gps_jitter():
    h1 = optimization_input_hash(
        optimization_provider="google",
        geometry_provider="google",
        delivery_ids=[3, 1, 2],
        stop_representative_ids=[10, 20],
        start=(-23.541234, -46.841198),
        end=None,
        priority=None,
        cost_objective="traveled_hour",
    )
    h2 = optimization_input_hash(
        optimization_provider="google",
        geometry_provider="google",
        delivery_ids=[1, 2, 3],
        stop_representative_ids=[10, 20],
        start=(-23.541201, -46.841155),
        end=None,
        priority=None,
        cost_objective="traveled_hour",
    )
    assert h1 == h2


def test_optimization_input_hash_changes_with_provider():
    base = dict(
        geometry_provider="google",
        delivery_ids=[1],
        stop_representative_ids=[1],
        start=None,
        end=None,
        priority=None,
        cost_objective="traveled_hour",
    )
    assert optimization_input_hash(optimization_provider="google", **base) != optimization_input_hash(
        optimization_provider="osrm", **base
    )


def test_order_hash_and_label():
    assert order_hash([1, 2, 3]) == order_hash([1, 2, 3])
    assert order_hash([1, 2, 3]) != order_hash([1, 3, 2])
    assert shipment_label(42) == "stop:repr=42"
