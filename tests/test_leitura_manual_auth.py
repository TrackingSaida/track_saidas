"""Origem de leitura: camera vs manual vs selecao; flags de motoboy."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from leitura_manual_auth import (
    ensure_lancar_avulso_allowed,
    ensure_manual_code_entry_allowed,
    normalize_origem_leitura,
    raise_if_selecao_sem_registro,
)


def test_normalize_aceita_selecao():
    assert normalize_origem_leitura("selecao") == "selecao"
    assert normalize_origem_leitura("manual") == "manual"
    assert normalize_origem_leitura("camera") == "camera"
    assert normalize_origem_leitura("outra") == "camera"


def test_motoboy_selecao_nao_exige_digitacao():
    user = SimpleNamespace(role=4, motoboy_id=11)
    db = MagicMock()
    origem = ensure_manual_code_entry_allowed(db, user, origem="selecao")
    assert origem == "selecao"
    db.get.assert_not_called()


def test_motoboy_camera_nao_consulta_flag_digitacao():
    user = SimpleNamespace(role=4, motoboy_id=11)
    db = MagicMock()
    origem = ensure_manual_code_entry_allowed(db, user, origem="camera")
    assert origem == "camera"
    db.get.assert_not_called()


def test_motoboy_manual_sem_flag_bloqueia():
    user = SimpleNamespace(role=4, motoboy_id=11)
    db = MagicMock()
    db.get.return_value = SimpleNamespace(pode_digitar_codigo_manual=False)
    with pytest.raises(HTTPException) as exc:
        ensure_manual_code_entry_allowed(db, user, origem="manual")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "MANUAL_CODE_ENTRY_FORBIDDEN"


def test_motoboy_manual_com_flag_permite():
    user = SimpleNamespace(role=4, motoboy_id=11)
    db = MagicMock()
    db.get.return_value = SimpleNamespace(pode_digitar_codigo_manual=True)
    origem = ensure_manual_code_entry_allowed(db, user, origem="manual")
    assert origem == "manual"


def test_staff_manual_sempre_permite():
    user = SimpleNamespace(role=2, motoboy_id=None)
    db = MagicMock()
    origem = ensure_manual_code_entry_allowed(db, user, origem="manual")
    assert origem == "manual"
    db.get.assert_not_called()


def test_selecao_sem_registro_retorna_404():
    with pytest.raises(HTTPException) as exc:
        raise_if_selecao_sem_registro("selecao")
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "AVULSO_NAO_ENCONTRADO"


def test_selecao_sem_registro_nao_afeta_camera():
    raise_if_selecao_sem_registro("camera")
    raise_if_selecao_sem_registro("manual")


def test_motoboy_sem_lancar_avulso_bloqueia_create():
    user = SimpleNamespace(role=4, motoboy_id=11)
    db = MagicMock()
    db.get.return_value = SimpleNamespace(pode_lancar_avulso=False)
    with pytest.raises(HTTPException) as exc:
        ensure_lancar_avulso_allowed(db, user)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "LANCAR_AVULSO_FORBIDDEN"


def test_staff_sempre_pode_lancar_avulso():
    user = SimpleNamespace(role=2, motoboy_id=None)
    db = MagicMock()
    ensure_lancar_avulso_allowed(db, user)
    db.get.assert_not_called()
