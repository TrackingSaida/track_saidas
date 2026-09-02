from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from coleta_operacional_service import combinar_modo_execucao, exigir_modo, modo_coleta
from base_fechamento_routes import _exigir_admin_financeiro
from routes_ui import menu_for_role


def _db_owner(*, ignorar=False, modo="codigo"):
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(
        ignorar_coleta=ignorar,
        modo_operacao=modo,
    )
    return db


def test_ignorar_coleta_desativa_todos_os_fluxos():
    db = _db_owner(ignorar=True, modo="ambos")
    assert modo_coleta(db, "SB") == "desativado"
    with pytest.raises(HTTPException) as exc:
        exigir_modo(db, "SB", "codigo")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("esperado", ["codigo", "coleta_manual"])
def test_modo_ambos_libera_leitura_e_manual(esperado):
    exigir_modo(_db_owner(modo="ambos"), "SB", esperado)


def test_modo_manual_bloqueia_leitura():
    with pytest.raises(HTTPException) as exc:
        exigir_modo(_db_owner(modo="coleta_manual"), "SB", "codigo")
    assert exc.value.status_code == 403


def test_execucao_combina_leitura_e_manual_sem_duplicar_base():
    assert combinar_modo_execucao("codigo", "coleta_manual") == "ambos"
    assert combinar_modo_execucao("coleta_manual", "codigo") == "ambos"
    assert combinar_modo_execucao("codigo", "codigo") == "codigo"


def _hrefs_menu(menu):
    return {item["href"] for secao in menu for item in secao["items"]}


def test_menu_ignorar_coleta_oculta_todos_os_itens_de_coleta():
    hrefs = _hrefs_menu(menu_for_role(1, ignorar_coleta=True, modo_operacao="coleta_manual"))
    assert "tracking-coleta-leitura.html" not in hrefs
    assert "tracking-coletas-resumo.html" not in hrefs
    assert "tracking-bases-a-receber.html" not in hrefs
    assert "dashboard-coletas.html" not in hrefs


def test_menu_manual_oculta_scanner_mas_mantem_gestao():
    hrefs = _hrefs_menu(menu_for_role(1, ignorar_coleta=False, modo_operacao="coleta_manual"))
    assert "tracking-coleta-leitura.html" not in hrefs
    assert "tracking-coletas-resumo.html" in hrefs
    assert "tracking-coletas-operacao.html" in hrefs


def test_menu_financeiro_separa_saidas_e_coletas():
    menu = menu_for_role(1, ignorar_coleta=False, modo_operacao="ambos")
    financeiro = next(secao for secao in menu if secao["section"] == "Financeiro")
    grupos = {item["href"]: item.get("group") for item in financeiro["items"]}
    assert grupos["tracking-entregadores-a-pagar.html"] == "saidas"
    assert grupos["tracking-entregadores-resumo.html"] == "saidas"
    assert grupos["tracking-bases-a-receber.html"] == "coletas"
    assert grupos["tracking-coletas-resumo.html"] == "coletas"


def test_baixa_a_receber_exige_admin_ou_root():
    _exigir_admin_financeiro(SimpleNamespace(role=0))
    _exigir_admin_financeiro(SimpleNamespace(role=1))
    with pytest.raises(HTTPException) as exc:
        _exigir_admin_financeiro(SimpleNamespace(role=4))
    assert exc.value.status_code == 403


def test_root_admin_permite_somente_roles_0_e_1():
    from coleta_operacional_routes import _root_admin

    assert _root_admin(SimpleNamespace(role=0)) is True
    assert _root_admin(SimpleNamespace(role=1)) is True
    assert _root_admin(SimpleNamespace(role=2)) is False
    assert _root_admin(SimpleNamespace(role=3)) is False
    assert _root_admin(SimpleNamespace(role=4)) is False


def test_valor_servicos_calcula_por_preco_da_base():
    from decimal import Decimal

    from coleta_operacional_routes import _valor_servicos

    base = SimpleNamespace(shopee=Decimal("2.50"), ml=Decimal("3.00"), avulso=Decimal("1.00"))
    assert _valor_servicos(base, shopee=2, mercado_livre=4, avulso=1) == Decimal("16.00")


def test_participante_sem_lancamento_e_liberacao_volta_pendente():
    from coleta_operacional_service import liberar_participacao_vazia, participante_sem_lancamento

    vazio = SimpleNamespace(
        shopee=0,
        mercado_livre=0,
        avulso=0,
        sem_volume=False,
        id_participante=1,
        status="em_coleta",
    )
    com_volume = SimpleNamespace(
        shopee=1,
        mercado_livre=0,
        avulso=0,
        sem_volume=False,
        id_participante=2,
        status="em_coleta",
    )
    assert participante_sem_lancamento(vazio) is True
    assert participante_sem_lancamento(com_volume) is False

    db = MagicMock()
    execucao = SimpleNamespace(participantes=[vazio], status="em_coleta", atualizado_em=None)
    assert liberar_participacao_vazia(db, execucao=execucao, participante=vazio) is True
    assert db.delete.call_count == 2
    db.delete.assert_any_call(vazio)
    db.delete.assert_any_call(execucao)

    db2 = MagicMock()
    execucao2 = SimpleNamespace(participantes=[com_volume], status="em_coleta", atualizado_em=None)
    assert liberar_participacao_vazia(db2, execucao=execucao2, participante=com_volume) is False
    db2.delete.assert_not_called()
