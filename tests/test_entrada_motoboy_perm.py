"""Gate de entrada para motoboy: owner ON + pode_realizar_entrada; avulso/manual."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


def test_staff_com_entrada_on_ok():
    from entrada_access import require_entrada_access

    db = MagicMock()
    user = SimpleNamespace(role=2, sub_base="SB", entrada_obrigatoria_habilitada=True, motoboy_id=None)
    assert require_entrada_access(db, user) == "SB"


def test_motoboy_sem_flag_403():
    from entrada_access import require_entrada_access

    db = MagicMock()
    db.scalar.return_value = 1
    db.get.return_value = SimpleNamespace(pode_realizar_entrada=False)
    user = SimpleNamespace(role=4, sub_base="SB", entrada_obrigatoria_habilitada=True, motoboy_id=10)
    with patch("entrada_access.owner_entrada_habilitada", return_value=True):
        with pytest.raises(HTTPException) as exc:
            require_entrada_access(db, user)
    assert exc.value.status_code == 403
    assert isinstance(exc.value.detail, dict) and exc.value.detail.get("code") == "ENTRADA_FORBIDDEN"


def test_motoboy_com_flag_ok():
    from entrada_access import require_entrada_access

    db = MagicMock()
    db.scalar.return_value = 1
    db.get.return_value = SimpleNamespace(pode_realizar_entrada=True)
    user = SimpleNamespace(role=4, sub_base="SB", entrada_obrigatoria_habilitada=True, motoboy_id=10)
    with patch("entrada_access.owner_entrada_habilitada", return_value=True):
        assert require_entrada_access(db, user) == "SB"


def test_motoboy_sub_base_invalida_403():
    from entrada_access import require_entrada_access

    db = MagicMock()
    db.scalar.return_value = None
    user = SimpleNamespace(role=4, sub_base="OUTRA", entrada_obrigatoria_habilitada=True, motoboy_id=10)
    with pytest.raises(HTTPException) as exc:
        require_entrada_access(db, user)
    assert exc.value.status_code == 403
    assert "Sub-base" in str(exc.value.detail)


def test_entrada_owner_off_403():
    from entrada_access import require_entrada_access

    db = MagicMock()
    user = SimpleNamespace(role=2, sub_base="SB", entrada_obrigatoria_habilitada=False, motoboy_id=None)
    with patch("entrada_access.owner_entrada_habilitada", return_value=False):
        with pytest.raises(HTTPException) as exc:
            require_entrada_access(db, user)
    assert exc.value.status_code == 403
    assert isinstance(exc.value.detail, dict) and exc.value.detail.get("code") == "ENTRADA_DESABILITADA"


def test_avulso_entrada_exige_criar_avulso_coleta():
    from leitura_manual_auth import ensure_lancar_avulso_allowed, motoboy_pode_criar_avulso

    so_saida = SimpleNamespace(pode_criar_avulso_coleta=False, pode_criar_avulso_saida=True, pode_lancar_avulso=True)
    assert motoboy_pode_criar_avulso(so_saida, "entrada") is False
    assert motoboy_pode_criar_avulso(so_saida, "coleta") is False
    assert motoboy_pode_criar_avulso(so_saida, "saida") is True

    com_coleta = SimpleNamespace(pode_criar_avulso_coleta=True, pode_criar_avulso_saida=False, pode_lancar_avulso=True)
    assert motoboy_pode_criar_avulso(com_coleta, "entrada") is True

    db = MagicMock()
    db.get.return_value = so_saida
    user = SimpleNamespace(role=4, motoboy_id=1)
    with pytest.raises(HTTPException) as exc:
        ensure_lancar_avulso_allowed(db, user, contexto="entrada")
    assert exc.value.status_code == 403


def test_cadastro_default_entrada_respeita_controle():
    from motoboy_cadastro_service import resolver_permissoes_motoboy

    owner_off = SimpleNamespace(
        default_pode_realizar_coleta=False,
        default_pode_realizar_entrada=True,
        entrada_obrigatoria_habilitada=False,
        default_pode_ler_saida=True,
        default_pode_digitar_codigo_manual=False,
        default_pode_lancar_avulso=True,
        default_pode_criar_avulso_coleta=True,
        default_pode_criar_avulso_saida=False,
        default_avulso_exige_foto=True,
        ignorar_coleta=False,
    )
    assert resolver_permissoes_motoboy(owner_off)["pode_realizar_entrada"] is False

    owner_on = SimpleNamespace(**{**owner_off.__dict__, "entrada_obrigatoria_habilitada": True})
    assert resolver_permissoes_motoboy(owner_on)["pode_realizar_entrada"] is True
