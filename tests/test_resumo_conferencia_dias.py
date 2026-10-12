"""Conferência por dia no resumo agrupado por motoboy (fechamento)."""
from datetime import date
from decimal import Decimal

import entregador_routes as er
from conferencia_saida_pure import status_conferencia_dia

INI, FIM = date(2026, 9, 1), date(2026, 9, 5)


def _dia(mid, data, feitos=3):
    q = {"qtde": feitos, "valor_unit": Decimal("1"), "total": Decimal(feitos)}
    zero = {"qtde": 0, "valor_unit": Decimal("0"), "total": Decimal("0")}
    return er.EntregadorResumoItem(
        data=data,
        motoboy_id=mid,
        entregador_nome=f"Moto {mid}",
        shopee=q,
        flex=zero,
        avulso=zero,
        total_dia=Decimal(feitos),
        total_feitos=feitos,
        valor_feitos=Decimal(feitos),
        fechamento_status="PENDENTE",
    )


def _preparar(monkeypatch, registros, chamadas):
    def conf(db, *, sub_base, motoboy_ids, periodo_inicio, periodo_fim):
        chamadas.append(sorted(motoboy_ids))
        return registros

    monkeypatch.setattr(er, "status_conferencia_registrada_motoboys", conf)
    monkeypatch.setattr(er, "_motoboys_com_diarias_coleta", lambda db, sb, ini, fim: [])
    monkeypatch.setattr(er, "_diarias_coleta_em_aberto_motoboys", lambda *a, **k: {})


def _resumo(lista, habilitada):
    return er._resumo_agrupado_por_executor(
        None,
        "sb",
        lista,
        data_inicio=INI,
        data_fim=FIM,
        fechamento_status=None,
        filtro_motoboy_id=None,
        filtro_entregador_id=None,
        page=1,
        pageSize=50,
        conferencia_habilitada=habilitada,
    )


def test_status_dia_sem_registro_conta_nao_conferido():
    assert status_conferencia_dia(None) == "pendente"
    assert status_conferencia_dia("pendente") == "pendente"
    assert status_conferencia_dia("RECONFERIR") == "reconferir"
    assert status_conferencia_dia("conferida") == "conferida"


def test_resumo_marca_dias_e_conta_sem_conferencia(monkeypatch):
    chamadas = []
    registros = {
        (1, date(2026, 9, 1)): "conferida",
        (1, date(2026, 9, 2)): "reconferir",
        (2, date(2026, 9, 1)): "conferida",
    }
    _preparar(monkeypatch, registros, chamadas)
    lista = [
        _dia(1, "2026-09-01"),
        _dia(1, "2026-09-02"),
        _dia(1, "2026-09-03"),
        _dia(1, "2026-09-04", feitos=0),
        _dia(2, "2026-09-01"),
    ]

    out = _resumo(lista, habilitada=True)

    assert out.conferencia_habilitada is True
    assert chamadas == [[1, 2]]
    por_id = {r.motoboy_id: r for r in out.items}
    m1 = por_id[1]
    assert (m1.dias_com_entrega, m1.dias_sem_conferencia) == (3, 2)
    assert [(d.data, d.conferencia_status, d.conferencia_label) for d in m1.dias] == [
        ("2026-09-01", "conferida", "Conferido"),
        ("2026-09-02", "reconferir", "Reconferir"),
        ("2026-09-03", "pendente", "Não conferido"),
        ("2026-09-04", None, None),
    ]
    assert (por_id[2].dias_com_entrega, por_id[2].dias_sem_conferencia) == (1, 0)


def test_resumo_sem_conferencia_habilitada_nao_consulta(monkeypatch):
    chamadas = []
    _preparar(monkeypatch, {}, chamadas)

    out = _resumo([_dia(1, "2026-09-01")], habilitada=False)

    assert out.conferencia_habilitada is False
    assert chamadas == []
    item = out.items[0]
    assert item.dias_sem_conferencia == 0
    assert item.dias[0].conferencia_status is None
