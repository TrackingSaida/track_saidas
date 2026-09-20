"""Flags de criar avulso por fluxo (Coleta vs Saída)."""
from types import SimpleNamespace

from leitura_manual_auth import motoboy_pode_criar_avulso, sync_motoboy_avulso_legado, sync_owner_avulso_defaults


def test_coleta_on_saida_off():
    m = SimpleNamespace(
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=False,
        pode_lancar_avulso=True,
        avulso_exige_foto=True,
    )
    assert motoboy_pode_criar_avulso(m, "coleta") is True
    assert motoboy_pode_criar_avulso(m, "saida") is False
    sync_motoboy_avulso_legado(m)
    assert m.pode_lancar_avulso is True
    assert m.avulso_exige_foto is True


def test_ambos_off_desliga_legado_e_foto():
    m = SimpleNamespace(
        pode_criar_avulso_coleta=False,
        pode_criar_avulso_saida=False,
        pode_lancar_avulso=True,
        avulso_exige_foto=True,
    )
    sync_motoboy_avulso_legado(m)
    assert m.pode_lancar_avulso is False
    assert m.avulso_exige_foto is False


def test_owner_defaults_or():
    owner = SimpleNamespace(
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=False,
        default_pode_lancar_avulso=False,
        default_avulso_exige_foto=True,
    )
    sync_owner_avulso_defaults(owner)
    assert owner.default_pode_lancar_avulso is True
    assert owner.default_avulso_exige_foto is True


def test_saida_false_nao_volta_pelo_legado_or():
    from politicas_routes import PadroesMotoboyPatch, _owner_to_out
    from leitura_manual_auth import apply_motoboy_avulso_padroes, apply_owner_avulso_padroes

    owner = SimpleNamespace(
        ignorar_coleta=False,
        modo_operacao="codigo",
        bloquear_saida_sem_coleta=False,
        entrada_obrigatoria_habilitada=False,
        conferencia_saida_habilitada=False,
        devolucao_sub_base_habilitada=False,
        default_pode_realizar_coleta=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=True,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    patch = PadroesMotoboyPatch(
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=False,
        pode_lancar_avulso=True,
    )
    flags = patch.model_dump(exclude_unset=True)
    apply_owner_avulso_padroes(
        owner,
        pode_criar_avulso_coleta=flags.get("pode_criar_avulso_coleta"),
        pode_criar_avulso_saida=flags.get("pode_criar_avulso_saida"),
        pode_lancar_avulso=flags.get("pode_lancar_avulso"),
    )
    assert owner.default_pode_criar_avulso_coleta is True
    assert owner.default_pode_criar_avulso_saida is False
    assert owner.default_pode_lancar_avulso is True

    pad = _owner_to_out(owner).padroes_motoboy
    dumped = pad.model_dump()
    assert dumped["pode_criar_avulso_coleta"] is True
    assert dumped["pode_criar_avulso_saida"] is False
    assert dumped["pode_lancar_avulso"] is True
    assert "pode_criar_avulso_saida" in dumped

    motoboy = SimpleNamespace(
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=True,
        pode_lancar_avulso=True,
        avulso_exige_foto=True,
    )
    apply_motoboy_avulso_padroes(
        motoboy,
        pode_criar_avulso_coleta=pad.pode_criar_avulso_coleta,
        pode_criar_avulso_saida=pad.pode_criar_avulso_saida,
    )
    assert motoboy.pode_criar_avulso_saida is False
    assert motoboy.pode_criar_avulso_coleta is True
    assert motoboy.pode_lancar_avulso is True


def test_patch_json_false_nao_vira_none():
    from politicas_routes import PadroesMotoboyPatch

    parsed = PadroesMotoboyPatch.model_validate(
        {"pode_criar_avulso_coleta": True, "pode_criar_avulso_saida": False}
    )
    assert parsed.pode_criar_avulso_saida is False
    assert parsed.pode_criar_avulso_coleta is True
    dumped = parsed.model_dump(exclude_unset=True)
    assert dumped["pode_criar_avulso_saida"] is False
    assert dumped["pode_criar_avulso_coleta"] is True
    assert "pode_lancar_avulso" not in dumped


def test_resolve_motoboy_saida_false_nao_usa_legado():
    from leitura_manual_auth import resolve_motoboy_avulso_flags

    m = SimpleNamespace(
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=False,
        pode_lancar_avulso=True,
    )
    coleta, saida = resolve_motoboy_avulso_flags(m)
    assert coleta is True
    assert saida is False


def test_aplicar_padrao_saida_false_em_motoboy_de_user_sub_base():
    from leitura_manual_auth import apply_motoboy_avulso_padroes, apply_owner_avulso_padroes, list_motoboys_da_sub_base

    owner = SimpleNamespace(
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=True,
        default_pode_lancar_avulso=True,
        default_avulso_exige_foto=True,
    )
    apply_owner_avulso_padroes(
        owner,
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=False,
    )
    motoboy = SimpleNamespace(
        id_motoboy=99,
        user_id=1,
        sub_base=None,
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=True,
        pode_lancar_avulso=True,
        avulso_exige_foto=True,
        claims_version=0,
    )
    apply_motoboy_avulso_padroes(
        motoboy,
        pode_criar_avulso_coleta=owner.default_pode_criar_avulso_coleta,
        pode_criar_avulso_saida=owner.default_pode_criar_avulso_saida,
    )
    assert motoboy.pode_criar_avulso_saida is False
    assert motoboy.pode_criar_avulso_coleta is True
    assert list_motoboys_da_sub_base is not None
    assert motoboy.sub_base is None
