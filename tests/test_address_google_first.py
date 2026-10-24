"""Google Places entra direto só quando o endereço não está no cadastro."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from address_providers.base import RawAddressHit
from address_providers.google_places_provider import (
    GoogleAutocompleteOutcome,
    GooglePlacesPrediction,
    google_direct_available,
    reset_session_google_cost_guard,
)

GOOGLE_ON = {
    "GOOGLE_PLACES_ENABLED": "true",
    "GOOGLE_PLACES_API_KEY": "key",
    "GOOGLE_PLACES_AUTO_FALLBACK": "true",
}


def _prediction() -> GooglePlacesPrediction:
    return GooglePlacesPrediction(
        place_id="p1",
        main_text="Rua Nova, 100",
        secondary_text="Centro, Barueri - SP",
        full_text="Rua Nova, 100, Centro, Barueri - SP",
    )


def _known_hit() -> RawAddressHit:
    return RawAddressHit(
        rua="Rua Cabo Frio",
        numero="43",
        bairro="Jardim Maria Helena",
        cidade="Barueri",
        estado="SP",
        cep="06442000",
        latitude=-23.511,
        longitude=-46.876,
        source="known",
    )


class TestGoogleDirectAvailable(unittest.TestCase):
    def tearDown(self):
        reset_session_google_cost_guard()

    @patch.dict(os.environ, {"GOOGLE_PLACES_ENABLED": "false", "GOOGLE_PLACES_API_KEY": "key"})
    def test_desligado_nao_chama(self):
        allowed, reason, guard = google_direct_available("sess")
        self.assertFalse(allowed)
        self.assertEqual(reason, "google_disabled")
        self.assertFalse(guard)

    @patch.dict(os.environ, {**GOOGLE_ON, "GOOGLE_PLACES_SESSION_LIMIT": "1"})
    def test_limite_da_sessao_bloqueia(self):
        from address_providers.google_places_provider import _record_session_google_call

        _record_session_google_call("sess")
        allowed, reason, guard = google_direct_available("sess")
        self.assertFalse(allowed)
        self.assertEqual(reason, "session_limit")
        self.assertTrue(guard)


class TestSearchGoogleFirst(unittest.TestCase):
    def tearDown(self):
        reset_session_google_cost_guard()

    def _search(self, **kwargs):
        from smart_address_search import SmartAddressSearch

        params = dict(
            db=MagicMock(),
            query="Rua Nova 100",
            sub_base="SB1",
            session_token="sess",
        )
        params.update(kwargs)
        return SmartAddressSearch().search(**params)

    @patch.dict(os.environ, GOOGLE_ON)
    @patch("smart_address_search.GooglePlacesClient")
    @patch("smart_address_search.get_cached", return_value=[{"label": "cache", "score": 90}])
    @patch("smart_address_search.build_did_you_mean", return_value=None)
    @patch("smart_address_search.log_address_event")
    def test_cache_nao_chama_google(self, _log, _dym, _cached, client):
        result = self._search()
        client.assert_not_called()
        self.assertFalse(result["used_google"])
        self.assertEqual(result["suggestions"][0]["label"], "cache")

    @patch.dict(os.environ, GOOGLE_ON)
    @patch("smart_address_search._fetch_provider_hits")
    @patch("smart_address_search.GooglePlacesClient")
    @patch("smart_address_search.score_hit", return_value=(80, None, None))
    @patch("smart_address_search.search_known", return_value=[(_known_hit(), 3)])
    @patch("smart_address_search.get_motoboy_stats", return_value=({}, {}))
    @patch("smart_address_search.get_sub_base_stats", return_value=({}, {}))
    @patch("smart_address_search.get_cached", return_value=None)
    @patch("smart_address_search.set_cached")
    @patch("smart_address_search.build_did_you_mean", return_value=None)
    @patch("smart_address_search.log_address_event")
    def test_endereco_conhecido_nao_chama_google(
        self, _log, _dym, _set_cached, _cached, _stats, _mot, _known, _score, client, fetch
    ):
        result = self._search(query="Rua Cabo Frio 43")
        client.assert_not_called()
        fetch.assert_not_called()
        self.assertFalse(result["used_google"])

    @patch.dict(os.environ, GOOGLE_ON)
    @patch("smart_address_search._fetch_provider_hits")
    @patch("smart_address_search.GooglePlacesClient")
    @patch("smart_address_search.search_known", return_value=[])
    @patch("smart_address_search.get_motoboy_stats", return_value=({}, {}))
    @patch("smart_address_search.get_sub_base_stats", return_value=({}, {}))
    @patch("smart_address_search.get_cached", return_value=None)
    @patch("smart_address_search.set_cached")
    @patch("smart_address_search.build_did_you_mean", return_value=None)
    @patch("smart_address_search.log_address_event")
    def test_endereco_novo_chama_google_antes_dos_outros(
        self, _log, _dym, _set_cached, _cached, _stats, _mot, _known, client_cls, fetch
    ):
        client = client_cls.return_value
        client.autocomplete.return_value = GoogleAutocompleteOutcome(
            predictions=[_prediction()],
            http_status=200,
            first_result="Rua Nova, 100",
        )
        result = self._search()
        client.autocomplete.assert_called_once()
        fetch.assert_not_called()
        self.assertTrue(result["used_google"])

    @patch.dict(os.environ, GOOGLE_ON)
    @patch("smart_address_search._fetch_provider_hits", return_value=([], False, {}))
    @patch("smart_address_search.GooglePlacesClient")
    @patch("smart_address_search.search_known", return_value=[])
    @patch("smart_address_search.get_motoboy_stats", return_value=({}, {}))
    @patch("smart_address_search.get_sub_base_stats", return_value=({}, {}))
    @patch("smart_address_search.get_cached", return_value=None)
    @patch("smart_address_search.build_did_you_mean", return_value=None)
    @patch("smart_address_search.log_address_event")
    def test_google_vazio_cai_nos_outros_provedores(
        self, _log, _dym, _cached, _stats, _mot, _known, client_cls, fetch
    ):
        client_cls.return_value.autocomplete.return_value = GoogleAutocompleteOutcome(
            predictions=[],
            http_status=200,
        )
        self._search()
        client_cls.return_value.autocomplete.assert_called_once()
        fetch.assert_called_once()
