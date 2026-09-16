"""Bloquear vs avisar na saída sem coleta."""
from __future__ import annotations

from types import SimpleNamespace

from politicas_routes import _owner_to_out
from saida_prerequisito import avaliar_prerequisito_saida


def test_bloquear_false_permite_soft():
    err = avaliar_prerequisito_saida(
        coleta_habilitada=True,
        entrada_habilitada=False,
        saida_existe=False,
        permitir_registrar_nao_coletado=True,
    )
    assert err is None


def test_bloquear_true_rejeita_mesmo_com_flag_payload():
    """Backend deve passar permitir=False quando owner.bloquear_saida_sem_coleta."""
    permitir_soft = True and not True  # payload True, owner bloquear True
    err = avaliar_prerequisito_saida(
        coleta_habilitada=True,
        entrada_habilitada=False,
        saida_existe=False,
        permitir_registrar_nao_coletado=permitir_soft,
    )
    assert err and err["code"] == "NAO_COLETADO"


def test_politicas_mapeia_bloquear_saida_sem_coleta():
    owner = SimpleNamespace(
        ignorar_coleta=False,
        modo_operacao="codigo",
        bloquear_saida_sem_coleta=True,
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        devolucao_sub_base_habilitada=False,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    out = _owner_to_out(owner)
    assert out.operacao.bloquear_saida_sem_coleta is True
    assert out.operacao.coleta_habilitada is True


def test_default_bloquear_false_preserva_comportamento_atual():
    owner = SimpleNamespace(
        ignorar_coleta=False,
        modo_operacao="codigo",
        # atributo ausente → getattr False
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        devolucao_sub_base_habilitada=False,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    out = _owner_to_out(owner)
    assert out.operacao.bloquear_saida_sem_coleta is False
