"""Testes de mapeamento status rota API."""
from datetime import date, datetime

from route_api_status import (
    API_STATUS_EM_ENTREGA,
    API_STATUS_ROTA_PRONTA,
    API_STATUS_SEM_ROTA,
    build_rotas_ativa_out,
    map_rota_to_api_status,
)


class _RotaStub:
    def __init__(self, status: str, finalizado_em=None, data=None, id=1, parada_atual=0, ordem=None):
        self.status = status
        self.finalizado_em = finalizado_em
        self.data = data or date.today()
        self.id = id
        self.parada_atual = parada_atual
        self.ordem_json = "[]"
        self.iniciado_em = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        self.sub_base = "TEST"


def test_map_sem_rota():
    assert map_rota_to_api_status(None) == API_STATUS_SEM_ROTA


def test_map_rota_pronta():
    rota = _RotaStub("preparando")
    assert map_rota_to_api_status(rota) == API_STATUS_ROTA_PRONTA


def test_map_em_entrega():
    rota = _RotaStub("ativa")
    assert map_rota_to_api_status(rota) == API_STATUS_EM_ENTREGA


def test_build_sem_rota():
    out = build_rotas_ativa_out(None, sub_base="SB", motoboy_id=1, data_iso="2026-06-16")
    assert out["status"] == API_STATUS_SEM_ROTA
    assert out["rota_id"] is None


def test_build_rota_pronta():
    rota = _RotaStub("preparando")
    out = build_rotas_ativa_out(rota, sub_base="SB", motoboy_id=1, data_iso="2026-06-16", ordem=[1, 2])
    assert out["status"] == API_STATUS_ROTA_PRONTA
    assert out["ordem"] == [1, 2]
