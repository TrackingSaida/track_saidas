"""Round-trip: PATCH saida=false replica a role=4 com Motoboy.sub_base nulo."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from types import SimpleNamespace
from unittest.mock import MagicMock

from leitura_manual_auth import (
    flush_owner_avulso_columns,
    list_users_role4_da_sub_base,
    resolve_owner_avulso_defaults,
)
from politicas_routes import PadroesMotoboyPatch, PoliticasPatch, _owner_to_out, patch_politicas
from users_routes_updated import _user_to_out


def _owner(**over):
    data = dict(
        id_owner=1,
        sub_base="BASE_X",
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
    data.update(over)
    return SimpleNamespace(**data)


def _motoboy(**over):
    data = dict(
        id_motoboy=10,
        user_id=5,
        sub_base=None,
        documento="123",
        cnpj=None,
        chave_pix=None,
        rua="Rua",
        numero="1",
        complemento=None,
        bairro="Centro",
        cidade="SP",
        estado="SP",
        cep="01000000",
        pode_ler_coleta=False,
        pode_realizar_coleta=False,
        pode_ler_saida=True,
        pode_digitar_codigo_manual=False,
        pode_criar_avulso_coleta=True,
        pode_criar_avulso_saida=True,
        pode_lancar_avulso=True,
        avulso_exige_foto=True,
        claims_version=0,
    )
    data.update(over)
    return SimpleNamespace(**data)


def _user_role4(*, motoboy=None, **over):
    data = dict(
        id=5,
        email=None,
        username="moto1",
        contato="11988887777",
        status=True,
        sub_base="BASE_X",
        nome="Ana",
        sobrenome="Silva",
        data_nascimento=None,
        role=4,
        coletador=False,
        motoboy=motoboy,
        must_change_password=True,
    )
    data.update(over)
    return SimpleNamespace(**data)


def _db(owner, users_role4):
    db = MagicMock()
    db.scalar.return_value = owner
    uniq = MagicMock()
    uniq.all.return_value = users_role4
    scalars = MagicMock()
    scalars.unique.return_value = uniq
    db.scalars.return_value = scalars
    return db


def test_list_users_role4_usa_user_sub_base_nao_motoboy_sub_base():
    captured = {}

    class DB:
        def scalars(self, stmt):
            captured["stmt"] = stmt
            mock = MagicMock()
            mock.unique.return_value.all.return_value = []
            return mock

    list_users_role4_da_sub_base(DB(), "BASE_X")
    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "base_x" in sql
    assert "role" in sql
    assert "motoboys.sub_base" not in sql


def test_user_to_out_role4_sem_perfil_nao_omite_flags():
    out = _user_to_out(_user_role4(motoboy=None))
    assert out.motoboy is not None
    dumped = out.motoboy.model_dump()
    assert dumped["pode_criar_avulso_saida"] is False
    assert dumped["pode_criar_avulso_coleta"] is False
    assert dumped["pode_lancar_avulso"] is False


def test_user_to_out_saida_false_nao_usa_legado():
    m = _motoboy(pode_criar_avulso_saida=False, pode_lancar_avulso=True)
    out = _user_to_out(_user_role4(motoboy=m))
    assert out.motoboy.pode_criar_avulso_saida is False
    assert out.motoboy.pode_criar_avulso_coleta is True
    assert out.motoboy.pode_lancar_avulso is True


def test_flush_owner_update_envia_saida_false():
    owner = _owner(
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=False,
        default_pode_lancar_avulso=True,
    )
    db = MagicMock()
    coleta, saida = flush_owner_avulso_columns(db, owner)
    assert coleta is True
    assert saida is False
    assert db.execute.called
    stmt = db.execute.call_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "default_pode_criar_avulso_saida" in compiled
    assert "false" in compiled


def test_patch_saida_false_aplica_role4_com_motoboy_sub_base_nulo():
    owner = _owner()
    motoboy = _motoboy(sub_base=None)
    user = _user_role4(motoboy=motoboy)
    sem_perfil = _user_role4(id=6, username="moto2", motoboy=None)
    db = _db(owner, [user, sem_perfil])
    admin = SimpleNamespace(role=1, sub_base="BASE_X")
    body = PoliticasPatch(
        padroes_motoboy=PadroesMotoboyPatch(
            pode_criar_avulso_coleta=True,
            pode_criar_avulso_saida=False,
        ),
        aplicar_padroes_aos_motoboys=True,
    )

    out = patch_politicas(body, db, admin)

    assert owner.default_pode_criar_avulso_saida is False
    assert motoboy.pode_criar_avulso_saida is False
    assert motoboy.sub_base is None
    assert out.padroes_motoboy.pode_criar_avulso_saida is False
    assert out.motoboys_atualizados == 1
    assert out.motoboys_sem_perfil == 1

    get_depois = _owner_to_out(owner)
    assert get_depois.padroes_motoboy.pode_criar_avulso_saida is False
    coleta, saida = resolve_owner_avulso_defaults(owner)
    assert coleta is True
    assert saida is False

    listed = _user_to_out(user)
    assert listed.motoboy.pode_criar_avulso_saida is False


def test_patch_sem_apply_nao_inventa_contagem():
    owner = _owner()
    db = _db(owner, [])
    admin = SimpleNamespace(role=1, sub_base="BASE_X")
    body = PoliticasPatch(
        padroes_motoboy=PadroesMotoboyPatch(
            pode_criar_avulso_coleta=True,
            pode_criar_avulso_saida=False,
        ),
        aplicar_padroes_aos_motoboys=False,
    )
    out = patch_politicas(body, db, admin)
    assert out.padroes_motoboy.pode_criar_avulso_saida is False
    assert out.motoboys_atualizados is None
    assert out.motoboys_sem_perfil is None


def test_patch_coleta_e_foto_false_para_true():
    """Ligar coleta + foto: resposta deve refletir True (não o estado stale)."""
    owner = _owner(
        default_pode_criar_avulso_coleta=False,
        default_pode_criar_avulso_saida=False,
        default_pode_lancar_avulso=False,
        default_avulso_exige_foto=False,
    )
    db = _db(owner, [])
    admin = SimpleNamespace(role=1, sub_base="BASE_X")
    body = PoliticasPatch(
        padroes_motoboy=PadroesMotoboyPatch(
            pode_criar_avulso_coleta=True,
            pode_criar_avulso_saida=False,
            avulso_exige_foto=True,
        ),
        aplicar_padroes_aos_motoboys=False,
    )
    out = patch_politicas(body, db, admin)
    assert owner.default_pode_criar_avulso_coleta is True
    assert owner.default_pode_criar_avulso_saida is False
    assert owner.default_avulso_exige_foto is True
    assert out.padroes_motoboy.pode_criar_avulso_coleta is True
    assert out.padroes_motoboy.pode_criar_avulso_saida is False
    assert out.padroes_motoboy.avulso_exige_foto is True
    assert out.padroes_motoboy.pode_lancar_avulso is True


def test_patch_coleta_true_nao_depende_de_refresh_stale():
    """Mesmo se expire/refresh viesse a corromper o objeto, a resposta usa memória do PATCH."""
    owner = _owner(
        default_pode_criar_avulso_coleta=False,
        default_pode_criar_avulso_saida=False,
        default_pode_lancar_avulso=False,
        default_avulso_exige_foto=False,
    )
    db = _db(owner, [])

    def expire_stale(obj, *args, **kwargs):
        # Simula reload stale que devolveria false (bug que remarcava coleta/foto).
        if obj is owner:
            owner.default_pode_criar_avulso_coleta = False
            owner.default_pode_criar_avulso_saida = False
            owner.default_pode_lancar_avulso = False
            owner.default_avulso_exige_foto = False

    def refresh_stale(obj, *args, **kwargs):
        expire_stale(obj)

    db.expire.side_effect = expire_stale
    db.refresh.side_effect = refresh_stale

    admin = SimpleNamespace(role=1, sub_base="BASE_X")
    body = PoliticasPatch(
        padroes_motoboy=PadroesMotoboyPatch(
            pode_criar_avulso_coleta=True,
            pode_criar_avulso_saida=False,
            avulso_exige_foto=True,
        ),
        aplicar_padroes_aos_motoboys=False,
    )
    out = patch_politicas(body, db, admin)

    # Sem expire/refresh no patch, memória permanece True e resposta também.
    assert owner.default_pode_criar_avulso_coleta is True
    assert owner.default_avulso_exige_foto is True
    assert out.padroes_motoboy.pode_criar_avulso_coleta is True
    assert out.padroes_motoboy.avulso_exige_foto is True
    db.expire.assert_not_called()
    db.refresh.assert_not_called()


def test_flush_owner_usa_synchronize_session_false():
    owner = _owner(
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=False,
    )
    db = MagicMock()
    flush_owner_avulso_columns(db, owner)
    stmt = db.execute.call_args.args[0]
    opts = getattr(stmt, "_execution_options", None) or {}
    assert opts.get("synchronize_session") is False