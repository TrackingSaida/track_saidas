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
