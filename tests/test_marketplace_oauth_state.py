"""State OAuth marketplace: encode/decode e compat com sub_base puro."""
from __future__ import annotations

import os

import pytest

# Garante SECRET_KEY antes de importar o módulo (auth exige no import).
os.environ.setdefault("SECRET_KEY", "test-secret-oauth-state-key-32chars!!")

from marketplace_oauth_state_pure import decode_oauth_state, encode_oauth_state


def test_encode_sem_id_base_devolve_sub_base_puro():
    assert encode_oauth_state("  minha_sub  ") == "minha_sub"
    assert decode_oauth_state("minha_sub") == ("minha_sub", None)


def test_encode_com_id_base_roundtrip():
    token = encode_oauth_state("op_xyz", 42)
    assert token != "op_xyz"
    assert "." in token
    sb, ib = decode_oauth_state(token)
    assert sb == "op_xyz"
    assert ib == 42


def test_decode_jwt_invalido_ou_vazio():
    assert decode_oauth_state("") == ("", None)
    assert decode_oauth_state(None) == ("", None)
    assert decode_oauth_state("a.b.c") == ("", None)
