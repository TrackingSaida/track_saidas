"""Agrupamento de paradas: bairro diferente não junta; endereço igualado junta."""
from types import SimpleNamespace

from route_stops import build_route_stops, build_stop_key


def _detail(**kwargs):
    defaults = dict(
        dest_cep="",
        dest_numero="",
        dest_rua="",
        dest_cidade="",
        dest_bairro="",
        endereco_formatado="",
        latitude=None,
        longitude=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_different_bairro_stays_two_stops():
    a = _detail(
        dest_rua="Av. Anibal Correia",
        dest_numero="193",
        dest_cidade="Barueri",
        dest_bairro="Jardim Paulista",
        dest_cep="06401-000",
    )
    b = _detail(
        dest_rua="Av. Anibal Correia",
        dest_numero="193",
        dest_cidade="Barueri",
        dest_bairro="Parque Viana",
        dest_cep="06449-000",
    )
    assert build_stop_key(a, 1) != build_stop_key(b, 2)
    stops = build_route_stops([1, 2], {1: a, 2: b})
    assert len(stops) == 2


def test_after_bairro_matches_same_address_groups():
    a = _detail(
        dest_rua="Av. Anibal Correia",
        dest_numero="193",
        dest_cidade="Barueri",
        dest_bairro="Jardim Paulista",
        dest_cep="06401-000",
    )
    b = _detail(
        dest_rua="Av. Anibal Correia",
        dest_numero="193",
        dest_cidade="Barueri",
        dest_bairro="Jardim Paulista",
        dest_cep="06449-000",
    )
    assert build_stop_key(a, 1) == build_stop_key(b, 2)
    stops = build_route_stops([1, 2], {1: a, 2: b})
    assert len(stops) == 1
    assert stops[0].delivery_ids == [1, 2]


def test_cep_key_used_when_street_missing():
    a = _detail(dest_cep="06010000", dest_numero="50")
    b = _detail(dest_cep="06010-000", dest_numero="50")
    assert build_stop_key(a, 1).startswith("cep|")
    assert build_stop_key(a, 1) == build_stop_key(b, 2)


def test_different_street_number_stay_separate():
    a = _detail(dest_rua="Rua A", dest_numero="10", dest_cidade="Osasco", dest_bairro="Centro")
    b = _detail(dest_rua="Rua A", dest_numero="20", dest_cidade="Osasco", dest_bairro="Centro")
    stops = build_route_stops([1, 2], {1: a, 2: b})
    assert len(stops) == 2
