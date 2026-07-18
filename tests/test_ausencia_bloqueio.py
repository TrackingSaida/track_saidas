from ausencia_bloqueio_service import MAX_AUSENCIAS, contar_ausencias_em_eventos


def test_bloqueio_apos_tres_ausencias():
    assert contar_ausencias_em_eventos(["ausente", "ausente"]) == 2
    assert contar_ausencias_em_eventos(["ausente", "ausente", "ausente"]) == 3
    assert contar_ausencias_em_eventos(["ausente", "ausente_lote", "ausente"]) >= MAX_AUSENCIAS


def test_liberacao_reinicia_contagem_efetiva():
    eventos = ["ausente", "ausente", "ausente", "liberacao_ausencias", "ausente"]
    assert contar_ausencias_em_eventos(eventos) == 1
