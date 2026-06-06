"""Testes unitários SmartAddressSearch (sem rede)."""
from __future__ import annotations

import unittest

from address_fuzzy import find_did_you_mean, similarity
from address_normalizer import normalizeAddressQuery, normalize_address_key, normalize_estado_uf
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from address_providers.base import RawAddressHit
from address_ranker import MIN_SUGGESTION_SCORE, RankContext, build_rank_context, score_hit, haversine_km


def _hit(**kwargs) -> RawAddressHit:
    defaults = dict(
        rua="Rua Cabo Frio",
        numero="43",
        bairro="Jardim Maria Helena",
        cidade="Barueri",
        estado="SP",
        cep="06442000",
        latitude=-23.511,
        longitude=-46.876,
        source="test",
    )
    defaults.update(kwargs)
    return RawAddressHit(**defaults)


class TestNormalizeAddressQuery(unittest.TestCase):
    def test_expand_abbreviations(self):
        self.assertEqual(normalizeAddressQuery("r cabo frio 43"), "Rua Cabo Frio 43")
        self.assertEqual(normalizeAddressQuery("av campinas"), "Avenida Campinas")
        self.assertEqual(normalizeAddressQuery("al rio negro"), "Alameda Rio Negro")


class TestGpsRanking(unittest.TestCase):
    def test_barueri_before_vinhedo(self):
        gps_lat, gps_lon = -23.511, -46.876
        barueri = _hit(cidade="Barueri", latitude=-23.511, longitude=-46.876)
        vinhedo = _hit(cidade="Vinhedo", latitude=-23.030, longitude=-46.975, bairro="Centro", cep="13280000")
        ctx = build_rank_context("Rua Cabo Frio 43", gps_lat=gps_lat, gps_lon=gps_lon)
        ctx.query_numero = "43"
        score_b, _, dist_b = score_hit(barueri, ctx)
        score_v, _, dist_v = score_hit(vinhedo, ctx)
        self.assertLess(dist_b, dist_v)
        self.assertGreater(score_b, score_v)

    def test_gps_distance_bands(self):
        ctx = RankContext(query="test", gps_lat=-23.51, gps_lon=-46.87)
        near = _hit(latitude=-23.512, longitude=-46.878)
        _, _, d_near = score_hit(near, ctx)
        self.assertLess(d_near, 3)
        far = _hit(latitude=-22.90, longitude=-47.50)
        score_far, _, d_far = score_hit(far, ctx)
        self.assertGreater(d_far, 40)


class TestCepAndNumero(unittest.TestCase):
    def test_cep_exact_bonus(self):
        ctx = RankContext(query="x", query_cep="06442000", query_numero="43")
        hit = _hit(cep="06442000", numero="43")
        score, _, _ = score_hit(hit, ctx)
        self.assertGreaterEqual(score, 40 + 50)

    def test_numero_mismatch_penalty(self):
        ctx = RankContext(query="x", query_numero="43")
        hit = _hit(numero="99")
        score_match, _, _ = score_hit(_hit(numero="43"), ctx)
        score_miss, _, _ = score_hit(hit, ctx)
        self.assertGreater(score_match, score_miss)


class TestMinScore(unittest.TestCase):
    def test_low_score_filtered(self):
        ctx = build_rank_context("Rua Cabo Frio 43", gps_lat=-23.51, gps_lon=-46.87)
        absurd = _hit(cidade="Campinas", latitude=-22.90, longitude=-47.06, estado="SP")
        score, _, _ = score_hit(absurd, ctx)
        self.assertLess(score, MIN_SUGGESTION_SCORE + 30)


class TestDedup(unittest.TestCase):
    def test_normalize_key_same_address(self):
        k1 = normalize_address_key("Rua Cabo Frio", "43", "06442-000")
        k2 = normalize_address_key("rua cabo frio", "43", "06442000")
        self.assertEqual(k1, k2)


class TestFuzzy(unittest.TestCase):
    def test_did_you_mean(self):
        candidates = [("Rua Cabo Frio", "Barueri", "SP")]
        match = find_did_you_mean("Rua Cabo Rio", candidates)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "Rua Cabo Frio")

    def test_similarity(self):
        self.assertGreater(similarity("Rua Cabo Rio", "Rua Cabo Frio"), 0.82)


class TestNormalizeEstadoUf(unittest.TestCase):
    def test_sao_paulo_nome_completo(self):
        self.assertEqual(normalize_estado_uf("São Paulo"), "SP")
        self.assertNotEqual(normalize_estado_uf("São Paulo"), "SÃ")

    def test_sigla_curta(self):
        self.assertEqual(normalize_estado_uf("SP"), "SP")

    def test_iso_br(self):
        self.assertEqual(normalize_estado_uf(None, iso3166="BR-SP"), "SP")


class TestHaversine(unittest.TestCase):
    def test_same_point_zero(self):
        self.assertAlmostEqual(haversine_km(-23.51, -46.87, -23.51, -46.87), 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
