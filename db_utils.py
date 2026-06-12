"""Utilitários de sessão SQLAlchemy."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session


def db_rollback_safe(db: Optional[Session]) -> None:
    if db is None:
        return
    try:
        db.rollback()
    except Exception:
        pass
