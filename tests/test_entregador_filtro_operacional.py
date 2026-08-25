"""Filtro operacional do fechamento: motoboy excluído não entra no dropdown."""
from entregador_legado_pure import entregador_aparece_no_filtro_operacional


def test_username_de_usuario_excluido_nao_aparece():
    assert entregador_aparece_no_filtro_operacional(
        username="joao",
        user_por_username_ativo=None,
        nome_tem_user_ativo=False,
    ) is False


def test_username_de_usuario_inativo_nao_aparece():
    assert entregador_aparece_no_filtro_operacional(
        username="joao",
        user_por_username_ativo=False,
        nome_tem_user_ativo=False,
    ) is False


def test_username_de_usuario_ativo_aparece():
    assert entregador_aparece_no_filtro_operacional(
        username="joao",
        user_por_username_ativo=True,
        nome_tem_user_ativo=True,
    ) is True


def test_username_ativo_nao_depende_do_nome():
    assert entregador_aparece_no_filtro_operacional(
        username="joao",
        user_por_username_ativo=True,
        nome_tem_user_ativo=False,
    ) is True


def test_username_inativo_ganha_mesmo_com_nome_ativo():
    assert entregador_aparece_no_filtro_operacional(
        username="joao",
        user_por_username_ativo=False,
        nome_tem_user_ativo=True,
    ) is False


def test_sem_username_orfao_de_exclusao_nao_aparece():
    """Espelho legado sem username, titular já excluído (hard delete)."""
    assert entregador_aparece_no_filtro_operacional(
        username=None,
        user_por_username_ativo=None,
        nome_tem_user_ativo=False,
    ) is False


def test_sem_username_com_motoboy_ativo_mesmo_nome_aparece():
    assert entregador_aparece_no_filtro_operacional(
        username="",
        user_por_username_ativo=None,
        nome_tem_user_ativo=True,
    ) is True
