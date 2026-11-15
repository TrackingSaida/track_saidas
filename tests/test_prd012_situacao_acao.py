"""PRD-012: eventos de sessão preservam Ação operacional legível."""

from saida_operacional_pure import EVENTOS_UI_ULTIMA_ACAO, rotulo_acao_evento


def test_eventos_de_sessao_participam_da_ultima_acao_operacional():
    assert {
        "leitura_saida",
        "sessao_leitura_finalizada",
        "saida_confirmada",
        "saida_removida_antes_confirmacao",
    }.issubset(EVENTOS_UI_ULTIMA_ACAO)


def test_eventos_de_sessao_tem_rotulos_explicitos():
    assert rotulo_acao_evento("leitura_saida") == "Leu pacote para saída"
    assert rotulo_acao_evento("sessao_leitura_finalizada") == "Finalizou leitura de saída"
    assert rotulo_acao_evento("saida_confirmada") == "Confirmou início da entrega"
    assert (
        rotulo_acao_evento("saida_removida_antes_confirmacao")
        == "Removeu antes de começar entrega"
    )
