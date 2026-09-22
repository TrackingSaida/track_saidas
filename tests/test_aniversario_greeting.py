from datetime import date, datetime
from zoneinfo import ZoneInfo

from aniversario_greeting import (
    ASSINATURA,
    build_aniversario_greeting,
    hoje_sao_paulo,
    is_aniversario_hoje,
    resolve_aniversario_payload,
)


def test_sem_data_nascimento_nao_e_aniversario():
    assert is_aniversario_hoje(None, hoje=date(2026, 3, 15)) is False
    assert resolve_aniversario_payload(None, 1, nome="Ana") is None


def test_dia_certo_e_errado():
    nasc = date(1990, 3, 15)
    assert is_aniversario_hoje(nasc, hoje=date(2026, 3, 15)) is True
    assert is_aniversario_hoje(nasc, hoje=date(2026, 3, 14)) is False
    assert is_aniversario_hoje(nasc, hoje=date(2026, 4, 15)) is False


def test_29_fevereiro_em_ano_nao_bissexto_celebra_em_28():
    nasc = date(2000, 2, 29)
    assert is_aniversario_hoje(nasc, hoje=date(2026, 2, 28)) is True
    assert is_aniversario_hoje(nasc, hoje=date(2026, 2, 27)) is False
    assert is_aniversario_hoje(nasc, hoje=date(2026, 3, 1)) is False


def test_29_fevereiro_em_ano_bissexto():
    nasc = date(2000, 2, 29)
    assert is_aniversario_hoje(nasc, hoje=date(2028, 2, 29)) is True
    assert is_aniversario_hoje(nasc, hoje=date(2028, 2, 28)) is False


def test_hoje_sao_paulo_respeita_fuso():
    # 15/03 02:00 UTC = ainda 14/03 em SP (UTC-3)
    utc = datetime(2026, 3, 15, 2, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert hoje_sao_paulo(utc) == date(2026, 3, 14)

    # 15/03 05:00 UTC = 15/03 02:00 em SP
    utc2 = datetime(2026, 3, 15, 5, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert hoje_sao_paulo(utc2) == date(2026, 3, 15)


def test_textos_admin_root():
    for role in (0, 1):
        g = build_aniversario_greeting(role, nome="Maria Silva")
        assert g.titulo == "Feliz aniversário, Maria! 🎉"
        assert "faz a operação acontecer" in g.mensagem
        assert g.mensagem.endswith(ASSINATURA)
        assert g.botao == "Acessar meu painel"


def test_textos_operador_coletador():
    for role in (2, 3):
        g = build_aniversario_greeting(role, nome="João")
        assert g.titulo == "Feliz aniversário, João! 🥳"
        assert "Antes de começar a operação" in g.mensagem
        assert ASSINATURA in g.mensagem
        assert g.botao == "Começar meu dia"


def test_textos_motoboy():
    g = build_aniversario_greeting(4, nome="Pedro")
    assert g.titulo == "Feliz aniversário, Pedro! 🛵"
    assert "primeira rota é especial" in g.mensagem
    assert ASSINATURA in g.mensagem
    assert g.botao == "Bora pra rota!"


def test_titulo_sem_nome_mantem_emoji():
    assert build_aniversario_greeting(1, nome=None).titulo == "Feliz aniversário! 🎉"
    assert build_aniversario_greeting(2, nome="").titulo == "Feliz aniversário! 🥳"
    assert build_aniversario_greeting(4, nome="  ").titulo == "Feliz aniversário! 🛵"


def test_resolve_payload_somente_no_dia():
    nasc = date(1995, 9, 22)
    assert resolve_aniversario_payload(nasc, 4, nome="Ana", hoje=date(2026, 9, 22)) is not None
    assert resolve_aniversario_payload(nasc, 4, nome="Ana", hoje=date(2026, 9, 21)) is None
