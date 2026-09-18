"""Testes unitários de avisos (sanitização e prioridade)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from avisos_service import MENSAGEM_MAX_LEN, normalize_prioridade, sanitize_mensagem


def test_sanitize_mensagem_ok_com_url():
    msg = "Veja https://exemplo.com/rota e http://foo.bar/x"
    assert sanitize_mensagem(msg) == msg


def test_sanitize_mensagem_remove_controle_e_trim():
    assert sanitize_mensagem("  olá\x00 mundo  ") == "olá mundo"


def test_sanitize_mensagem_rejeita_vazia():
    with pytest.raises(ValueError):
        sanitize_mensagem("   ")


def test_sanitize_mensagem_rejeita_acima_do_limite():
    with pytest.raises(ValueError):
        sanitize_mensagem("x" * (MENSAGEM_MAX_LEN + 1))


def test_normalize_prioridade():
    assert normalize_prioridade("URGENTE") == "urgente"
    assert normalize_prioridade(None) == "normal"
    with pytest.raises(HTTPException) as exc:
        normalize_prioridade("alta")
    assert exc.value.status_code == 400


def test_liberacao_e_bloqueio_no_always_send_source():
    """Garante que os tipos novos estão no frozenset do serviço de push (sem importar models/db)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "push_notification_service.py").read_text(encoding="utf-8")
    assert '"liberacao_ausencia"' in src
    assert '"bloqueio_ausencia"' in src
    assert "ALWAYS_SEND_TYPES" in src
