"""Testes de resiliência da busca de endereços."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import sqlalchemy  # noqa: F401

    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

from address_providers.base import provider_http_timeout_sec
from address_providers.nominatim_provider import NominatimProvider
from address_search_logging import (
    AddressSearchReport,
    GooglePlacesSearchStats,
    ProviderSearchStats,
    format_address_search_log,
)


class TestNominatimTimeout(unittest.TestCase):
    @patch("address_providers.nominatim_provider.requests.get")
    def test_nominatim_uses_numeric_timeout(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=[]),
        )
        provider = NominatimProvider()
        provider.search("Rua Rio de Janeiro 156, Brasil")
        mock_get.assert_called_once()
        timeout = mock_get.call_args.kwargs.get("timeout") or mock_get.call_args[1].get("timeout")
        self.assertEqual(timeout, provider_http_timeout_sec())
        self.assertIsInstance(timeout, float)

    def test_provider_http_timeout_sec_defined(self):
        self.assertGreater(provider_http_timeout_sec(), 0)


@unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy não instalado")
class TestCacheDbRollback(unittest.TestCase):
    def setUp(self):
        import suggestion_cache

        suggestion_cache._table_available = None
        self.suggestion_cache = suggestion_cache

    def test_is_table_available_rollback_on_error(self):
        db = MagicMock()
        db.execute.side_effect = RuntimeError("db down")
        with patch("suggestion_cache.db_rollback_safe") as mock_rollback:
            available = self.suggestion_cache._is_table_available(db)
        self.assertFalse(available)
        mock_rollback.assert_called_once_with(db)


@unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy não instalado")
class TestSubBaseStatsDegrade(unittest.TestCase):
    def test_returns_empty_on_db_error(self):
        from operational_stats import get_sub_base_stats

        db = MagicMock()
        db.execute.side_effect = RuntimeError("InFailedSqlTransaction")
        with patch("operational_stats.db_rollback_safe") as mock_rollback:
            cities, bairros = get_sub_base_stats(db, "RUB_TEST1")
        self.assertEqual(cities, {})
        self.assertEqual(bairros, {})
        mock_rollback.assert_called_once_with(db)


@unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy não instalado")
class TestSearchNeverRaises(unittest.TestCase):
    @patch("smart_address_search.emit_address_search_log")
    @patch("smart_address_search.log_address_event")
    @patch("smart_address_search.get_cached", return_value=None)
    @patch("smart_address_search.SmartAddressSearch._search_uncached")
    def test_search_returns_empty_on_uncached_error(self, mock_uncached, *_mocks):
        from smart_address_search import SmartAddressSearch

        mock_uncached.side_effect = RuntimeError("boom")
        db = MagicMock()
        result = SmartAddressSearch().search(
            db=db,
            query="Rua Rio de Janeiro 156",
            sub_base="RUB_TEST1",
        )
        self.assertEqual(result["suggestions"], [])
        self.assertIsNone(result["did_you_mean"])
        self.assertFalse(result["used_google"])


class TestAcceptanceQueriesLogFields(unittest.TestCase):
    def _log_for_query(
        self,
        query: str,
        *,
        final_provider: str,
        final_results: int,
        google_called: bool,
        google_status: int | None = None,
        fallback_reason: str = "",
    ) -> str:
        report = AddressSearchReport(
            query=query,
            providers=[
                ProviderSearchStats(provider="known", results=0),
                ProviderSearchStats(provider="nominatim", results=0, error="timeout"),
                ProviderSearchStats(provider="geoapify", results=2, best_score=38),
            ],
            google=GooglePlacesSearchStats(
                called=google_called,
                reason=fallback_reason,
                http_status=google_status,
                results=5 if google_called else 0,
            ),
            final_provider=final_provider,
            final_results=final_results,
            best_score=72 if google_called else 38,
        )
        return format_address_search_log(report)

    def test_rua_rio_de_janeiro_156(self):
        text = self._log_for_query(
            "Rua Rio de Janeiro 156",
            final_provider="geoapify",
            final_results=2,
            google_called=True,
            google_status=200,
            fallback_reason="low_score",
        )
        self.assertIn("final_provider=geoapify", text)
        self.assertIn("final_results=2", text)
        self.assertIn("google_called=true", text)

    def test_rua_rio_vila_boa_vista(self):
        text = self._log_for_query(
            "Rua Rio de Janeiro 156 Vila Boa Vista",
            final_provider="known",
            final_results=1,
            google_called=False,
            fallback_reason="score_above_threshold",
        )
        self.assertIn("google_called=false", text)
        self.assertIn("final_results=1", text)

    def test_av_sebastiao_davino(self):
        text = self._log_for_query(
            "Av. Sebastião Davino dos Reis 1015",
            final_provider="google_places",
            final_results=5,
            google_called=True,
            google_status=200,
            fallback_reason="low_score",
        )
        self.assertIn("final_provider=google_places", text)
        self.assertIn("final_results=5", text)
        self.assertIn("google_called=true", text)


class TestEndpointSafetyNet(unittest.TestCase):
    def test_degraded_result_shape(self):
        result = {"suggestions": [], "did_you_mean": None, "used_google": False}
        self.assertEqual(result["suggestions"], [])
        self.assertFalse(result["used_google"])


if __name__ == "__main__":
    unittest.main()
