"""Coleta usa o perfil do usuário autenticado, não uma ficha de motoboy residual."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/test")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from coleta_operacional_service import resolver_executor


class _Db:
    def __init__(self, user, motoboy):
        self._user = user
        self._motoboy = motoboy

    def get(self, _model, _id):
        return self._user

    def scalar(self, _stmt):
        return self._motoboy


def test_admin_nao_herda_bloqueio_de_motoboy():
    admin = SimpleNamespace(id=1, role=1, status=True, sub_base="base-a")
    ficha = SimpleNamespace(id_motoboy=9, pode_realizar_coleta=False)
    executor, motoboy_id = resolver_executor(_Db(admin, ficha), admin)
    assert executor is admin
    assert motoboy_id is None


def test_motoboy_sem_permissao_continua_bloqueado():
    motoboy_user = SimpleNamespace(id=2, role=4, status=True, sub_base="base-a")
    ficha = SimpleNamespace(id_motoboy=9, pode_realizar_coleta=False)
    with pytest.raises(HTTPException) as exc:
        resolver_executor(_Db(motoboy_user, ficha), motoboy_user)
    assert exc.value.status_code == 403


def test_motoboy_com_permissao_registra():
    motoboy_user = SimpleNamespace(id=2, role=4, status=True, sub_base="base-a")
    ficha = SimpleNamespace(id_motoboy=9, pode_realizar_coleta=True)
    _executor, motoboy_id = resolver_executor(_Db(motoboy_user, ficha), motoboy_user)
    assert motoboy_id == 9
