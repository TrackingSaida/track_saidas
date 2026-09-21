"""resolve_avulso_exige_foto: política global também vale para staff."""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from types import SimpleNamespace
from unittest.mock import MagicMock

from leitura_manual_auth import resolve_avulso_exige_foto


def test_global_foto_obriga_mesmo_sem_motoboy():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(default_avulso_exige_foto=True)
    assert resolve_avulso_exige_foto(db, sub_base="RUB_TEST1", motoboy=None) is True


def test_global_off_motoboy_on():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(default_avulso_exige_foto=False)
    motoboy = SimpleNamespace(avulso_exige_foto=True)
    assert resolve_avulso_exige_foto(db, sub_base="RUB_TEST1", motoboy=motoboy) is True


def test_global_and_motoboy_off():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(default_avulso_exige_foto=False)
    motoboy = SimpleNamespace(avulso_exige_foto=False)
    assert resolve_avulso_exige_foto(db, sub_base="RUB_TEST1", motoboy=motoboy) is False


def test_global_on_ignores_motoboy_false():
    db = MagicMock()
    db.scalar.return_value = SimpleNamespace(default_avulso_exige_foto=True)
    motoboy = SimpleNamespace(avulso_exige_foto=False)
    assert resolve_avulso_exige_foto(db, sub_base="Giro", motoboy=motoboy) is True
