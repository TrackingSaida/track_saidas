"""Marcar como pago em lote: um recálculo e push fora da resposta."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import entregador_fechamento_routes as routes
import motoboy_nome_utils

INI, FIM = date(2026, 9, 1), date(2026, 9, 15)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    def scalars(self, _stmt):
        return _Scalars(self.rows)

    def commit(self):
        self.commits += 1


class _BG:
    def __init__(self):
        self.tasks = []

    def add_task(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))


def _fech(fid, mid, valor="10.00", status="GERADO"):
    return SimpleNamespace(
        id_fechamento=fid,
        id_motoboy=mid,
        id_entregador=None,
        valor_base=Decimal(valor),
        valor_final=Decimal(valor),
        status=status,
        username_entregador=f"m{mid}",
        periodo_inicio=INI,
        periodo_fim=FIM,
    )


def _payload(**extra):
    data = dict(
        periodo_inicio=INI,
        periodo_fim=FIM,
        ids_fechamento=[1, 2],
        confirmar_com_divergencia=True,
    )
    data.update(extra)
    return routes.MarcarPagoRequest(**data)


def test_recalculo_de_lote_consulta_o_periodo_uma_vez(monkeypatch):
    chamadas = []

    def fake_valores(db, sub_base, ids, ini, fim):
        chamadas.append((sub_base, list(ids), ini, fim))
        return {int(i): Decimal("8.00") for i in ids}

    monkeypatch.setattr(routes, "valores_base_atuais_motoboys", fake_valores)
    alvos = [_fech(1, 10), _fech(2, 11)]

    out = routes._valores_base_recalculados(None, "sb", alvos)

    assert chamadas == [("sb", [10, 11], INI, FIM)]
    assert out == {1: Decimal("8.00"), 2: Decimal("8.00")}


def test_legado_sem_motoboy_continua_individual(monkeypatch):
    monkeypatch.setattr(
        routes,
        "valores_base_atuais_motoboys",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("lote nao deve rodar")),
    )
    monkeypatch.setattr(routes, "_recalcular_valor_base_fechamento", lambda db, sb, fech: Decimal("3.50"))
    alvo = _fech(9, None)

    assert routes._valores_base_recalculados(None, "sb", [alvo]) == {9: Decimal("3.50")}


def test_marcar_pago_grava_e_deixa_push_para_depois(monkeypatch):
    pushes = []
    monkeypatch.setattr(routes, "_resolve_user_base", lambda db, user: "base-a")
    monkeypatch.setattr(
        routes,
        "_valores_base_recalculados",
        lambda db, sb, alvos: {int(f.id_fechamento): f.valor_base for f in alvos},
    )
    monkeypatch.setattr(routes, "_enviar_push_fechamento_pago", lambda *a, **k: pushes.append("push"))

    fechs = [_fech(1, 10), _fech(2, 11, status="REAJUSTADO")]
    db = _DB(fechs)
    bg = _BG()
    resp = routes.marcar_fechamentos_pagos(_payload(), bg, db, SimpleNamespace(username="ana", id=7))

    assert resp.marcados == 2
    assert resp.ids_fechamento == [1, 2]
    assert db.commits == 1
    assert [f.status for f in fechs] == ["PAGO", "PAGO"]
    assert fechs[0].pago_por == "ana"
    assert pushes == []
    assert bg.tasks[0][0] is routes._notificar_pagamentos
    assert bg.tasks[0][1] == ("base-a", [1, 2])


def test_divergencia_sem_confirmacao_nao_grava(monkeypatch):
    monkeypatch.setattr(routes, "_resolve_user_base", lambda db, user: "base-a")
    monkeypatch.setattr(routes, "_valores_base_recalculados", lambda db, sb, alvos: {1: Decimal("12.00")})
    monkeypatch.setattr(
        routes,
        "_resolver_nome_exibicao_fechamento",
        lambda db, fech, nomes_motoboy_map=None: "Joao",
    )
    monkeypatch.setattr(motoboy_nome_utils, "carregar_nomes_motoboy_ids", lambda db, ids: {})

    fech = _fech(1, 10)
    db = _DB([fech])
    with pytest.raises(HTTPException) as exc:
        routes.marcar_fechamentos_pagos(
            _payload(ids_fechamento=[1], confirmar_com_divergencia=False),
            _BG(),
            db,
            SimpleNamespace(username="ana", id=7),
        )

    assert exc.value.status_code == 409
    assert db.commits == 0
    assert fech.status == "GERADO"
