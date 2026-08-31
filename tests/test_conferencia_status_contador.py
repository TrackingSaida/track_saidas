"""Contador de conferência: lidos do dia sem cancelado/encerrado."""
from types import SimpleNamespace

from conferencia_saida_pure import (
    filtrar_status_conferencia,
    montar_dias_conferencia_periodo,
    status_conta_na_conferencia,
)


def test_status_conta_na_conferencia_exclui_cancelado_e_encerrado():
    assert status_conta_na_conferencia("SAIU_PARA_ENTREGA") is True
    assert status_conta_na_conferencia("EM_ROTA") is True
    assert status_conta_na_conferencia("ENTREGUE") is True
    assert status_conta_na_conferencia("AUSENTE") is True
    assert status_conta_na_conferencia("saiu") is True
    assert status_conta_na_conferencia("CANCELADO") is False
    assert status_conta_na_conferencia("cancelado") is False
    assert status_conta_na_conferencia("cancelada") is False
    assert status_conta_na_conferencia("ENCERRADO_SISTEMA") is False
    assert status_conta_na_conferencia("encerrado pelo sistema") is False


def test_filtrar_status_remove_cancelados_e_mantem_entregues():
    rows = [
        SimpleNamespace(id_saida=1, status="SAIU_PARA_ENTREGA"),
        SimpleNamespace(id_saida=2, status="CANCELADO"),
        SimpleNamespace(id_saida=3, status="ENTREGUE"),
        SimpleNamespace(id_saida=4, status="cancelado"),
        SimpleNamespace(id_saida=5, status="AUSENTE"),
        SimpleNamespace(id_saida=6, status="ENCERRADO_SISTEMA"),
    ]
    filtradas = filtrar_status_conferencia(rows)
    assert [s.id_saida for s in filtradas] == [1, 3, 5]


def test_saidas_por_motoboy_mesmo_filtro_exclui_um_ml_cancelado():
    """Mario Sergio: 5 Shopee + 20 ML lidos, 1 ML cancelado → 24 / 5 / 19."""
    rows = (
        [SimpleNamespace(status="SAIU_PARA_ENTREGA", servico="shopee") for _ in range(5)]
        + [SimpleNamespace(status="EM_ROTA", servico="ml") for _ in range(19)]
        + [SimpleNamespace(status="CANCELADO", servico="ml")]
    )
    validas = filtrar_status_conferencia(rows)
    assert len(validas) == 24
    assert sum(1 for s in validas if s.servico == "shopee") == 5
    assert sum(1 for s in validas if s.servico == "ml") == 19


def test_dia_operacional_sem_registro_nao_conferido():
    dias = montar_dias_conferencia_periodo([], ["2026-08-01", "2026-08-02"])
    assert [d["data"] for d in dias] == ["2026-08-01", "2026-08-02"]
    assert all(d["conferido"] is False for d in dias)
    assert all(d["label"] == "Não conferido" for d in dias)


def test_somente_status_conferida_conta_como_conferido():
    dias = montar_dias_conferencia_periodo(
        [
            {"data": "2026-08-01", "status": "conferida"},
            {"data": "2026-08-02", "status": "pendente"},
            {"data": "2026-08-03", "status": "reconferir"},
        ],
        ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"],
    )
    by_date = {d["data"]: d for d in dias}
    assert by_date["2026-08-01"]["conferido"] is True
    assert by_date["2026-08-01"]["label"] == "Conferido"
    assert by_date["2026-08-02"]["label"] == "Não conferido"
    assert by_date["2026-08-03"]["label"] == "Não conferido"
    assert by_date["2026-08-04"]["label"] == "Não conferido"


def test_registro_fora_dos_dias_operacionais_ainda_aparece():
    dias = montar_dias_conferencia_periodo(
        [{"data": "2026-08-10", "status": "conferida"}],
        ["2026-08-01"],
    )
    assert [d["data"] for d in dias] == ["2026-08-01", "2026-08-10"]
    assert dias[0]["conferido"] is False
    assert dias[1]["conferido"] is True
