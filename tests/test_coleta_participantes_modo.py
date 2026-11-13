"""Gate de participantes na coleta (n vs um) e liberação vazia → Pendente."""
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_participantes_modo_owner_default_n():
    from coleta_operacional_service import participantes_modo_owner

    assert participantes_modo_owner(SimpleNamespace()) == "n"
    assert participantes_modo_owner(SimpleNamespace(coleta_participantes_modo=None)) == "n"
    assert participantes_modo_owner(SimpleNamespace(coleta_participantes_modo="")) == "n"
    assert participantes_modo_owner(SimpleNamespace(coleta_participantes_modo="UM")) == "um"
    assert participantes_modo_owner(SimpleNamespace(coleta_participantes_modo="n")) == "n"
    assert participantes_modo_owner(SimpleNamespace(coleta_participantes_modo="outro")) == "n"


def test_modo_n_sem_ajudar_nao_bloqueia():
    from coleta_operacional_service import exige_confirmacao_ajudar

    assert (
        exige_confirmacao_ajudar(
            modo="n",
            ja_participa=False,
            outros_ativos=["alice"],
            ajudar=False,
            retroativo=False,
        )
        is False
    )


def test_modo_um_sem_ajudar_retorna_409_gate():
    from coleta_operacional_service import exige_confirmacao_ajudar

    assert (
        exige_confirmacao_ajudar(
            modo="um",
            ja_participa=False,
            outros_ativos=["alice"],
            ajudar=False,
            retroativo=False,
        )
        is True
    )


def test_modo_um_com_ajudar_ok():
    from coleta_operacional_service import exige_confirmacao_ajudar

    assert (
        exige_confirmacao_ajudar(
            modo="um",
            ja_participa=False,
            outros_ativos=["alice"],
            ajudar=True,
            retroativo=False,
        )
        is False
    )


def test_liberar_vazio_remove_execucao_pendente():
    from coleta_operacional_service import liberar_participacao_vazia, participante_sem_lancamento

    vazio = SimpleNamespace(
        shopee=0,
        mercado_livre=0,
        avulso=0,
        sem_volume=False,
        id_participante=1,
        status="em_coleta",
    )
    assert participante_sem_lancamento(vazio) is True
    db = MagicMock()
    execucao = SimpleNamespace(participantes=[vazio], status="em_coleta", atualizado_em=None)
    assert liberar_participacao_vazia(db, execucao=execucao, participante=vazio) is True
    assert db.delete.call_count == 2
    db.delete.assert_any_call(vazio)
    db.delete.assert_any_call(execucao)
