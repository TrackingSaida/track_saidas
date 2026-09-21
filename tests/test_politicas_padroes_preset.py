"""Preset oficial e apply de padrões do motoboy (User.sub_base + MotoboySubBase)."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from types import SimpleNamespace
from unittest.mock import MagicMock

from leitura_manual_auth import list_motoboys_da_sub_base
from politicas_routes import PadroesMotoboyPoliticas, PoliticasPatch, PadroesMotoboyPatch, patch_politicas


def test_padroes_motoboy_politicas_preset_oficial():
    pad = PadroesMotoboyPoliticas()
    assert pad.pode_realizar_coleta is False
    assert pad.pode_ler_saida is True
    assert pad.pode_digitar_codigo_manual is False
    assert pad.pode_criar_avulso_coleta is True
    assert pad.pode_criar_avulso_saida is False
    assert pad.pode_lancar_avulso is True
    assert pad.avulso_exige_foto is True


def test_list_motoboys_inclui_vinculo_motoboy_sub_base(monkeypatch):
    import leitura_manual_auth as mod

    monkeypatch.setattr(mod, "list_users_role4_da_sub_base", lambda db, sub: [])
    db = MagicMock()
    m_extra = SimpleNamespace(id_motoboy=99)
    result = MagicMock()
    result.unique.return_value.all.return_value = [m_extra]
    db.scalars.return_value = result

    out = list_motoboys_da_sub_base(db, "Giro")
    assert len(out) == 1
    assert out[0].id_motoboy == 99


def test_list_motoboys_dedupe_user_e_vinculo(monkeypatch):
    import leitura_manual_auth as mod

    m_same = SimpleNamespace(id_motoboy=7)
    user = SimpleNamespace(motoboy=m_same)
    monkeypatch.setattr(mod, "list_users_role4_da_sub_base", lambda db, sub: [user])
    db = MagicMock()
    result = MagicMock()
    result.unique.return_value.all.return_value = [m_same]
    db.scalars.return_value = result

    out = list_motoboys_da_sub_base(db, "RUB_TEST1")
    assert len(out) == 1
    assert out[0].id_motoboy == 7


def test_patch_aplicar_atualiza_motoboys_via_list(monkeypatch):
    owner = SimpleNamespace(
        id_owner=1,
        sub_base="RUB_TEST1",
        ignorar_coleta=False,
        modo_operacao="codigo",
        bloquear_saida_sem_coleta=False,
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        devolucao_sub_base_habilitada=False,
        etiqueta_limite_diario_default=50,
        etiqueta_expiracao_dias=30,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=False,
        default_avulso_exige_foto=True,
    )
    motoboy = SimpleNamespace(
        id_motoboy=10,
        pode_realizar_coleta=True,
        pode_ler_coleta=True,
        pode_ler_saida=False,
        pode_digitar_codigo_manual=True,
        pode_lancar_avulso=True,
        pode_criar_avulso_coleta=False,
        pode_criar_avulso_saida=True,
        avulso_exige_foto=False,
        claims_version=0,
    )
    db = MagicMock()
    user = SimpleNamespace(id=1, role=0, sub_base="RUB_TEST1")

    monkeypatch.setattr("politicas_routes._assert_admin", lambda u: None)
    monkeypatch.setattr("politicas_routes._owner_for_user", lambda db, u: owner)
    monkeypatch.setattr("politicas_routes.list_users_role4_da_sub_base", lambda db, sub: [])
    monkeypatch.setattr("politicas_routes.list_motoboys_da_sub_base", lambda db, sub: [motoboy])
    monkeypatch.setattr("politicas_routes.flush_owner_avulso_columns", lambda db, o: (True, False))
    monkeypatch.setattr("politicas_routes.flush_motoboy_avulso_columns", lambda db, m: None)
    monkeypatch.setattr(
        "politicas_routes.replace_regioes",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "politicas_routes.listar_cobertura_estruturada",
        lambda *a, **k: {"regioes": [], "modo": "ilimitado", "prefixos_sem_regiao": []},
    )
    monkeypatch.setattr("politicas_routes.list_prefixos_ativos", lambda *a, **k: [])

    body = PoliticasPatch(
        padroes_motoboy=PadroesMotoboyPatch(
            pode_realizar_coleta=False,
            pode_ler_saida=True,
            pode_digitar_codigo_manual=False,
            pode_criar_avulso_coleta=True,
            pode_criar_avulso_saida=False,
            avulso_exige_foto=True,
        ),
        aplicar_padroes_aos_motoboys=True,
    )
    out = patch_politicas(body, db=db, current_user=user)
    assert out.motoboys_atualizados == 1
    assert motoboy.pode_ler_saida is True
    assert motoboy.pode_criar_avulso_coleta is True
    assert motoboy.pode_criar_avulso_saida is False
    assert motoboy.avulso_exige_foto is True
    assert out.padroes_motoboy.pode_criar_avulso_coleta is True
    assert out.padroes_motoboy.pode_criar_avulso_saida is False
    assert out.padroes_motoboy.avulso_exige_foto is True
