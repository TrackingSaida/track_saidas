"""Testes Google Places provider (sem rede)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from address_providers.google_places_provider import (
    GooglePlacesClient,
    parse_address_components,
    prediction_to_provisional_score,
    should_auto_invoke_google_places,
    GooglePlacesPrediction,
)


class TestParseAddressComponents(unittest.TestCase):
    def test_brazilian_address(self):
        components = [
            {"types": ["route"], "longText": "Avenida Sebastião Davino dos Reis"},
            {"types": ["street_number"], "longText": "1015"},
            {"types": ["sublocality"], "longText": "Centro"},
            {"types": ["locality"], "longText": "Sorocaba"},
            {"types": ["administrative_area_level_1"], "shortText": "SP"},
            {"types": ["postal_code"], "longText": "18040-000"},
        ]
        parsed = parse_address_components(components, "Av. Sebastião Davino dos Reis, 1015")
        self.assertEqual(parsed.rua, "Avenida Sebastião Davino dos Reis")
        self.assertEqual(parsed.numero, "1015")
        self.assertEqual(parsed.cidade, "Sorocaba")
        self.assertEqual(parsed.estado, "SP")


class TestShouldAutoInvokeGoogle(unittest.TestCase):
    @patch.dict(os.environ, {"GOOGLE_PLACES_ENABLED": "false", "GOOGLE_PLACES_API_KEY": "key"})
    def test_disabled_never_calls(self):
        ok, reason, guard = should_auto_invoke_google_places([], "Rua X 10")
        self.assertFalse(ok)
        self.assertFalse(guard)
        self.assertEqual(reason, "google_disabled")

    @patch.dict(
        os.environ,
        {"GOOGLE_PLACES_ENABLED": "true", "GOOGLE_PLACES_API_KEY": "key", "GOOGLE_PLACES_AUTO_FALLBACK": "false"},
    )
    def test_auto_fallback_flag_off(self):
        ok, reason, _ = should_auto_invoke_google_places([], "Rua X 10")
        self.assertFalse(ok)
        self.assertEqual(reason, "auto_fallback_off")

    @patch.dict(
        os.environ,
        {"GOOGLE_PLACES_ENABLED": "true", "GOOGLE_PLACES_API_KEY": "key", "GOOGLE_PLACES_AUTO_FALLBACK": "true"},
    )
    def test_auto_fallback_when_no_results(self):
        ok, reason, guard = should_auto_invoke_google_places([], "Rua X 10")
        self.assertTrue(ok)
        self.assertEqual(reason, "no_results")
        self.assertFalse(guard)

    @patch.dict(
        os.environ,
        {
            "GOOGLE_PLACES_ENABLED": "true",
            "GOOGLE_PLACES_API_KEY": "key",
            "GOOGLE_PLACES_AUTO_FALLBACK": "true",
            "GOOGLE_AUTO_FALLBACK_MIN_SCORE": "60",
        },
    )
    def test_low_score_triggers_google(self):
        ok, reason, _ = should_auto_invoke_google_places(
            [{"score": 30, "rua": "Rua A", "numero": "1"}],
            "Rua A 1",
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "street_number_weak")

    @patch.dict(
        os.environ,
        {
            "GOOGLE_PLACES_ENABLED": "true",
            "GOOGLE_PLACES_API_KEY": "key",
            "GOOGLE_PLACES_AUTO_FALLBACK": "true",
            "GOOGLE_PLACES_SESSION_LIMIT": "1",
        },
    )
    def test_session_cost_guard(self):
        from address_providers.google_places_provider import (
            _record_session_google_call,
            reset_session_google_cost_guard,
        )

        reset_session_google_cost_guard()
        token = "sess-test-1"
        _record_session_google_call(token)
        ok, reason, guard = should_auto_invoke_google_places([], "Rua Y 20", session_token=token)
        self.assertFalse(ok)
        self.assertTrue(guard)
        self.assertEqual(reason, "session_limit")


class TestGooglePlacesClient(unittest.TestCase):
    @patch.dict(os.environ, {"GOOGLE_PLACES_API_KEY": "test-key"})
    @patch("address_providers.google_places_provider.requests.post")
    def test_autocomplete_parses_predictions(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(
                return_value={
                    "suggestions": [
                        {
                            "placePrediction": {
                                "placeId": "ChIJabc123",
                                "text": {"text": "Av. Sebastião Davino dos Reis, 1015, Sorocaba"},
                                "structuredFormat": {
                                    "mainText": {"text": "Av. Sebastião Davino dos Reis, 1015"},
                                    "secondaryText": {"text": "Sorocaba - SP"},
                                },
                                "distanceMeters": 1200,
                            }
                        }
                    ]
                }
            ),
        )
        client = GooglePlacesClient()
        outcome = client.autocomplete("Av. Sebastião Davino dos Reis 1015", session_token="sess-1")
        self.assertEqual(len(outcome.predictions), 1)
        self.assertEqual(outcome.http_status, 200)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.predictions[0].place_id, "ChIJabc123")
        self.assertEqual(outcome.predictions[0].distance_meters, 1200)
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        self.assertEqual(body["sessionToken"], "sess-1")
        self.assertEqual(body["regionCode"], "BR")

    @patch.dict(os.environ, {"GOOGLE_PLACES_API_KEY": "test-key"})
    @patch("address_providers.google_places_provider.requests.get")
    def test_place_details_returns_hit(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(
                return_value={
                    "id": "places/ChIJabc123",
                    "formattedAddress": "Av. Sebastião Davino dos Reis, 1015 - Centro, Sorocaba - SP",
                    "location": {"latitude": -23.501, "longitude": -47.458},
                    "addressComponents": [
                        {"types": ["route"], "longText": "Avenida Sebastião Davino dos Reis"},
                        {"types": ["street_number"], "longText": "1015"},
                        {"types": ["locality"], "longText": "Sorocaba"},
                        {"types": ["administrative_area_level_1"], "shortText": "SP"},
                    ],
                }
            ),
        )
        client = GooglePlacesClient()
        hit = client.get_place_details("ChIJabc123", session_token="sess-1")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.source, "google_places")
        self.assertEqual(hit.numero, "1015")
        self.assertAlmostEqual(hit.latitude, -23.501)


class TestProvisionalScore(unittest.TestCase):
    def test_distance_bonus(self):
        pred = GooglePlacesPrediction(
            place_id="x",
            main_text="Av. Sebastião Davino dos Reis, 1015",
            secondary_text="Sorocaba",
            full_text="full",
            distance_meters=1500,
        )
        score = prediction_to_provisional_score(pred, "Av. Sebastião Davino dos Reis 1015")
        self.assertGreater(score, 50)


if __name__ == "__main__":
    unittest.main()
