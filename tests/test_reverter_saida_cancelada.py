from types import SimpleNamespace

from saida_status_finalizado_pure import (
    aplicar_flag_cancelado_cobranca,
    is_root_admin,
    mensagem_bloqueio_cancelado,
    pode_alterar_pedido_cancelado,
    resolver_status_antes_do_cancelamento,
)


def test_is_root_admin_somente_roles_0_e_1():
    assert is_root_admin(0) is True
    assert is_root_admin(1) is True
    assert is_root_admin(2) is False
    assert is_root_admin(4) is False
    assert is_root_admin(None) is False
    assert is_root_admin("x") is False


def test_pode_alterar_pedido_cancelado_so_root_admin():
    assert pode_alterar_pedido_cancelado(0) is True
    assert pode_alterar_pedido_cancelado(1) is True
    assert pode_alterar_pedido_cancelado(2) is False
    assert pode_alterar_pedido_cancelado(3) is False
    assert pode_alterar_pedido_cancelado(4) is False


def test_mensagem_bloqueio_orienta_pedir_admin():
    msg = mensagem_bloqueio_cancelado()
    assert "root ou admin" in msg
    assert "cancelado" in msg.lower()


def test_cobranca_marca_cancelado_ao_cancelar():
    item = SimpleNamespace(fechado=False, cancelado=False)
    aplicar_flag_cancelado_cobranca([item], "saiu", "CANCELADO")
    assert item.cancelado is True


def test_cobranca_reabre_item_aberto_ao_reverter_cancelado():
    item = SimpleNamespace(fechado=False, cancelado=True)
    aplicar_flag_cancelado_cobranca([item], "CANCELADO", "saiu")
    assert item.cancelado is False


def test_cobranca_nao_reabre_item_ja_fechado():
    item = SimpleNamespace(fechado=True, cancelado=True)
    aplicar_flag_cancelado_cobranca([item], "CANCELADO", "saiu")
    assert item.cancelado is True


def test_cobranca_ignora_quando_status_nao_muda():
    item = SimpleNamespace(fechado=False, cancelado=True)
    aplicar_flag_cancelado_cobranca([item], "CANCELADO", "CANCELADO")
    assert item.cancelado is True


def test_resolver_status_antes_do_cancelamento_usa_ultimo_evento():
    eventos = [
        {"evento": "lido", "status_anterior": None, "status_novo": "saiu"},
        {"evento": "entregue", "status_anterior": "saiu", "status_novo": "ENTREGUE"},
        {"evento": "cancelado", "status_anterior": "ENTREGUE", "status_novo": "CANCELADO"},
    ]
    assert resolver_status_antes_do_cancelamento(eventos) == "ENTREGUE"


def test_resolver_status_antes_do_cancelamento_ignora_anterior_cancelado():
    eventos = [
        {"evento": "cancelado", "status_anterior": "CANCELADO", "status_novo": "CANCELADO"},
        {"evento": "cancelado", "status_anterior": "AUSENTE", "status_novo": "CANCELADO"},
    ]
    assert resolver_status_antes_do_cancelamento(eventos) == "AUSENTE"


def test_resolver_status_antes_do_cancelamento_sem_historico():
    assert resolver_status_antes_do_cancelamento([]) is None
