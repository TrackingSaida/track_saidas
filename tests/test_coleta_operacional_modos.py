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
