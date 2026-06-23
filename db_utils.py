"""Utilitários de sessão SQLAlchemy."""
from __future__ import annotations

from typing import Callable, Optional, TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

T = TypeVar("T")


def db_rollback_safe(db: Optional[Session]) -> None:
    if db is None:
        return
    try:
        db.rollback()
    except Exception:
        pass


def is_transient_db_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", exc)
    msg = str(orig or exc).lower()
    return any(
        token in msg
        for token in (
            "ssl connection has been closed",
            "connection reset",
            "server closed the connection",
            "could not receive data",
            "connection timed out",
            "broken pipe",
            "consuming input failed",
        )
    )


def run_db_query_with_retry(db: Session, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except (OperationalError, DBAPIError) as exc:
        if not is_transient_db_error(exc):
            raise
        db_rollback_safe(db)
        return fn()
