"""Helpers puros de avisos (sem I/O) — usados pelas rotas e testes."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException

MENSAGEM_MAX_LEN = 1000
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_mensagem(value: str) -> str:
    """Remove caracteres de controle; HTML livre não é permitido (clientes fazem escape + autolink)."""
    text = _CTRL_CHARS_RE.sub("", (value or "")).strip()
    if not text:
        raise ValueError("mensagem obrigatória")
    if len(text) > MENSAGEM_MAX_LEN:
        raise ValueError(f"mensagem deve ter no máximo {MENSAGEM_MAX_LEN} caracteres")
    return text


def normalize_prioridade(raw: Optional[str]) -> str:
    prioridade = (raw or "normal").strip().lower()
    if prioridade not in ("normal", "urgente"):
        raise HTTPException(400, "prioridade deve ser normal ou urgente.")
    return prioridade
