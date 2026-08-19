"""Contador de conferência: lidos do dia sem cancelado/encerrado."""
from types import SimpleNamespace

from conferencia_saida_pure import filtrar_status_conferencia, status_conta_na_conferencia


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
