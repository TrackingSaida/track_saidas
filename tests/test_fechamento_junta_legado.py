"""Entregador antigo da mesma pessoa não vira uma segunda linha no fechamento."""
from datetime import date
from types import SimpleNamespace

import entregador_routes as er


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self, _stmt):
        return _Scalars(self.rows)


def _dia(nome, data="2026-06-20", eid=7, cancelados=5, fechamento=None):
    return SimpleNamespace(
        data=data,
        entregador_nome=nome,
        entregador_id=eid,
        motoboy_id=None,
        id_fechamento=fechamento,
        fechamento_status="PENDENTE",
        periodo_inicio=None,
        periodo_fim=None,
        total_cancelado=cancelados,
    )


def _linhas():
    return {
        ("m", 10): {"nome": "Guilherme Messias", "dias": []},
        ("e", 7): {"nome": "Guilherme Messias", "dias": [_dia("Guilherme Messias")]},
    }


def test_junta_pelo_vinculo_de_usuario(monkeypatch):
    monkeypatch.setattr(
        er,
        "_resolve_executor_scope_ids",
        lambda db, sb, entregador_id=None, motoboy_id=None: ({7}, {10}),
    )
    fech = SimpleNamespace(
        status="GERADO",
        id_fechamento=3,
        periodo_inicio=date(2026, 6, 18),
        periodo_fim=date(2026, 6, 30),
    )
    linhas = _linhas()
    er._juntar_legado_ao_motoboy(_DB([fech]), "sb", linhas, {})

    assert ("e", 7) not in linhas
    dias = linhas[("m", 10)]["dias"]
    assert len(dias) == 1
    assert dias[0].motoboy_id == 10
    assert dias[0].fechamento_status == "GERADO"
    assert dias[0].id_fechamento == 3


def test_junta_pelo_nome_quando_nao_ha_vinculo(monkeypatch):
    monkeypatch.setattr(
        er,
        "_resolve_executor_scope_ids",
        lambda db, sb, entregador_id=None, motoboy_id=None: (set(), set()),
    )
    linhas = _linhas()
    er._juntar_legado_ao_motoboy(_DB([]), "sb", linhas, {})

    assert ("e", 7) not in linhas
    assert linhas[("m", 10)]["dias"][0].fechamento_status == "PENDENTE"


def test_nao_junta_nome_ambiguo(monkeypatch):
    monkeypatch.setattr(
        er,
        "_resolve_executor_scope_ids",
        lambda db, sb, entregador_id=None, motoboy_id=None: (set(), set()),
    )
    linhas = _linhas()
    linhas[("m", 11)] = {"nome": "Guilherme Messias", "dias": []}
    er._juntar_legado_ao_motoboy(_DB([]), "sb", linhas, {})

    assert ("e", 7) in linhas


def test_nao_junta_legado_que_ja_tem_fechamento(monkeypatch):
    monkeypatch.setattr(
        er,
        "_resolve_executor_scope_ids",
        lambda db, sb, entregador_id=None, motoboy_id=None: ({7}, {10}),
    )
    linhas = _linhas()
    linhas[("e", 7)]["dias"][0].id_fechamento = 99
    er._juntar_legado_ao_motoboy(_DB([]), "sb", linhas, {})

    assert ("e", 7) in linhas
    assert linhas[("m", 10)]["dias"] == []
