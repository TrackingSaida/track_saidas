"""Testes de logging estruturado da busca de endereços."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from address_search_logging import (
    AddressSearchReport,
    GooglePlacesSearchStats,
    ProviderSearchStats,
    format_address_search_log,
)
from address_providers.google_places_provider import (
    GooglePlacesClient,
    _parse_google_error,
    should_auto_invoke_google_places,
)


class TestFormatAddressSearchLog(unittest.TestCase):
    def test_geoapify_and_google_fallback(self):
        report = AddressSearchReport(
            query="Rua Rio de Janeiro 156",
            latitude=-23.51,
            longitude=-46.87,
            providers=[
                ProviderSearchStats(provider="known", results=0),
                ProviderSearchStats(provider="nominatim", results=0),
                ProviderSearchStats(provider="geoapify", results=2, best_score=38),
            ],
            google=GooglePlacesSearchStats(
                called=True,
                reason="low_score",
                http_status=200,
                results=5,
                first_result="Rua Rio de Janeiro, 156, Sorocaba - SP",
            ),
            final_provider="google_places",
            final_results=5,
            best_score=72,
        )
        text = format_address_search_log(report)
        self.assertIn("[address-search]", text)
        self.assertIn('query="Rua Rio de Janeiro 156"', text)
        self.assertIn("provider=geoapify", text)
        self.assertIn("score=38", text)
        self.assertIn("google_called=true", text)
        self.assertIn("google_places_called=true", text)
        self.assertIn("fallback_reason=\"low_score\"", text)
        self.assertIn("google_status=200", text)
        self.assertIn("google_places_status=200", text)
        self.assertIn("google_places_results=5", text)
        self.assertIn("final_provider=google_places", text)
        self.assertIn("best_score=72", text)

    def test_score_above_threshold(self):
        report = AddressSearchReport(
            query="Av. Paulista 1000",
            providers=[
                ProviderSearchStats(provider="known", results=0),
                ProviderSearchStats(provider="geoapify", results=3, best_score=78),
            ],
            google=GooglePlacesSearchStats(called=False, reason="score_above_threshold"),
            final_provider="geoapify",
            final_results=3,
            best_score=78,
        )
        text = format_address_search_log(report)
        self.assertIn("google_called=false", text)
        self.assertIn("fallback_reason=\"score_above_threshold\"", text)
        self.assertIn("final_provider=geoapify", text)
        self.assertIn("best_score=78", text)


class TestParseGoogleError(unittest.TestCase):
    def test_typical_google_error_body(self):
        response = MagicMock()
        response.json.return_value = {
            "error": {
                "code": 403,
                "message": "Requests from this API are blocked.",
                "status": "REQUEST_DENIED",
            }
        }
        self.assertIn("REQUEST_DENIED", _parse_google_error(response))


class TestGoogleAutocompleteHttpError(unittest.TestCase):
    @patch.dict(os.environ, {"GOOGLE_PLACES_API_KEY": "test-key"})
    @patch("address_providers.google_places_provider.requests.post")
    def test_http_403_returns_outcome_with_error(self, mock_post):
        import requests

        response = MagicMock()
        response.status_code = 403
        response.json.return_value = {
            "error": {"status": "REQUEST_DENIED", "message": "API key not valid"}
        }
        response.text = ""
        mock_post.return_value = response
        mock_post.return_value.raise_for_status.side_effect = requests.HTTPError(response=response)

        client = GooglePlacesClient()
        outcome = client.autocomplete("Rua X 10")
        self.assertEqual(outcome.http_status, 403)
        self.assertIn("REQUEST_DENIED", outcome.error or "")
        self.assertEqual(len(outcome.predictions), 0)
        self.assertIsNone(outcome.first_result)


class TestShouldAutoInvokeReasons(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "GOOGLE_PLACES_ENABLED": "true",
            "GOOGLE_PLACES_API_KEY": "key",
            "GOOGLE_PLACES_AUTO_FALLBACK": "true",
            "GOOGLE_AUTO_FALLBACK_MIN_SCORE": "60",
        },
    )
    def test_score_above_threshold_reason(self):
        ok, reason, guard = should_auto_invoke_google_places(
            [{"score": 78, "source": "geoapify"}],
            "Av. Paulista 1000",
        )
        self.assertFalse(ok)
        self.assertFalse(guard)
        self.assertEqual(reason, "score_above_threshold")

    @patch.dict(os.environ, {"GOOGLE_PLACES_ENABLED": "false", "GOOGLE_PLACES_API_KEY": "key"})
    def test_google_disabled_reason(self):
        ok, reason, _ = should_auto_invoke_google_places([], "Rua X")
        self.assertFalse(ok)
        self.assertEqual(reason, "google_disabled")


if __name__ == "__main__":
    unittest.main()
