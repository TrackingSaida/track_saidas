"""Testes de precisão / validação de geocode (Google + ranking)."""
from __future__ import annotations

import os
from unittest.mock import patch

from geocode_utils import (
    _google_formatted_matches_place,
    _infer_nominatim_precision,
    geocode_address_any,
)


def test_google_formatted_matches_city_and_uf():
    formatted = (
        "Rua Jorge Zanardo, 74 - Vila Marcondes, Carapicuíba - SP, 06334-000, Brasil"
    )
    assert _google_formatted_matches_place(
        formatted, cidade="Carapicuíba", estado="SP"
    )
    assert not _google_formatted_matches_place(
        formatted, cidade="Osasco", estado="SP"
    )


def test_infer_nominatim_precision_with_expected_number():
    with_house = {
        "type": "house",
        "address": {"house_number": "74"},
    }
    assert _infer_nominatim_precision(with_house, "74") == "rooftop"

    street_only = {
        "type": "road",
        "address": {},
    }
    assert _infer_nominatim_precision(street_only, "74") == "approx"


def test_geocode_address_any_uses_google_when_enabled():
    fake = (-23.55, -46.63, "rooftop")
    with patch.dict(
        os.environ,
        {
            "GOOGLE_GEOCODING_ENABLED": "true",
            "GOOGLE_GEOCODING_API_KEY": "test-key",
        },
        clear=False,
    ):
        with patch("geocode_utils.get_cached", return_value=None):
            with patch("geocode_utils.set_cached"):
                with patch("geocode_utils._geocode_with_google", return_value=fake) as mock_g:
                    result = geocode_address_any(
                        "Rua Teste, 10, São Paulo, SP, Brasil",
                        expected_numero="10",
                        cidade="São Paulo",
                        estado="SP",
                    )
                    assert result == fake
                    mock_g.assert_called_once()
                    kwargs = mock_g.call_args.kwargs
                    assert kwargs.get("cidade") == "São Paulo"
                    assert kwargs.get("estado") == "SP"


def test_cache_hit_google_with_number_is_rooftop():
    with patch(
        "geocode_utils.get_cached",
        return_value=(-23.5, -46.6, "google"),
    ):
        result = geocode_address_any(
            "Rua Teste, 10, São Paulo, SP",
            expected_numero="10",
        )
        assert result == (-23.5, -46.6, "rooftop")
