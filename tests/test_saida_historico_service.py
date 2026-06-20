"""Testes do serviço de histórico de saída."""
from datetime import datetime

from saida_historico_service import (
    EntregaHistoricoItemOut,
    SaidaHistoricoItemOut,
    projetar_historico_mobile,
)


def test_projetar_historico_mobile_omite_campos_admin():
    ts = datetime(2026, 6, 13, 0, 45, 0)
    full = [
        SaidaHistoricoItemOut(
            id=1,
            id_saida=99,
            evento="em_rota",
            timestamp=ts,
            status_anterior="SAIU_PARA_ENTREGA",
            status_novo="EM_ROTA",
            user_id=7,
            usuario_nome="motoboy1",
            motoboy_id_anterior=1,
            motoboy_id_novo=2,
            acao_label="Enviado para entrega",
        )
    ]
    mobile = projetar_historico_mobile(full)
    assert len(mobile) == 1
    item = mobile[0]
    assert isinstance(item, EntregaHistoricoItemOut)
    assert item.id == 1
    assert item.evento == "em_rota"
    assert item.timestamp == ts
    assert item.usuario_nome == "motoboy1"
    assert item.acao_label == "Enviado para entrega"
    dumped = item.model_dump()
    assert "status_anterior" not in dumped
    assert "motoboy_id_anterior" not in dumped
    assert "id_saida" not in dumped
