"""Instrumentação de performance desligável (PRD-017 / Fase 0).

Liga com PERF_INSTRUMENTATION=1 (ou true/yes/on).
Amostragem opcional: PERF_INSTRUMENTATION_SAMPLE_RATE entre 0 e 1 (default 1.0).

Não grava token, senha, código de pacote, id de usuário, motoboy ou owner.
Não atribui CPU do processo a uma request individual.
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger("perf")

_TRUTH = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in _TRUTH


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


PERF_INSTRUMENTATION_ENABLED = _env_flag("PERF_INSTRUMENTATION", False)
PERF_INSTRUMENTATION_SAMPLE_RATE = _env_float("PERF_INSTRUMENTATION_SAMPLE_RATE", 1.0)

_request_ctx: ContextVar[Optional["PerfRequestContext"]] = ContextVar(
    "perf_request_ctx", default=None
)
_engine_listeners_attached = False


@dataclass
class PerfRequestContext:
    request_id: str
    method: str
    path_template: str
    started: float = field(default_factory=time.perf_counter)
    sql_count: int = 0
    sql_ms: float = 0.0
    recalc_count: int = 0
    recalc_rows: int = 0
    pool_checkout_ms: float = 0.0
    pool_checkouts: int = 0
    sampled: bool = True


def get_perf_context() -> Optional[PerfRequestContext]:
    return _request_ctx.get()


def note_recalc(*, rows: int = 0) -> None:
    """Incrementa contadores de recálculo na request atual, se instrumentada."""
    ctx = _request_ctx.get()
    if ctx is None or not ctx.sampled:
        return
    ctx.recalc_count += 1
    if rows > 0:
        ctx.recalc_rows += int(rows)


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    parts = []
    for part in path.split("/"):
        if not part:
            continue
        if part.isdigit() or (
            len(part) > 24 and all(c.isalnum() or c in "-_" for c in part)
        ):
            parts.append(":id")
        else:
            parts.append(part)
    return "/" + "/".join(parts) if parts else "/"


def _should_sample() -> bool:
    if not PERF_INSTRUMENTATION_ENABLED:
        return False
    rate = PERF_INSTRUMENTATION_SAMPLE_RATE
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return random.random() < rate


def attach_engine_listeners(engine: Any) -> None:
    """Registra listeners de SQL e pool uma vez. Seguro chamar várias vezes."""
    global _engine_listeners_attached
    if _engine_listeners_attached:
        return
    if not PERF_INSTRUMENTATION_ENABLED:
        return

    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        context._perf_sql_start = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        ctx = _request_ctx.get()
        if ctx is None or not ctx.sampled:
            return
        started = getattr(context, "_perf_sql_start", None)
        if started is None:
            return
        ctx.sql_count += 1
        ctx.sql_ms += (time.perf_counter() - started) * 1000.0

    @event.listens_for(engine, "checkout")
    def _checkout(dbapi_conn, connection_record, connection_proxy):
        connection_record.info["_perf_checkout_at"] = time.perf_counter()
        ctx = _request_ctx.get()
        if ctx is not None and ctx.sampled:
            ctx.pool_checkouts += 1

    @event.listens_for(engine, "checkin")
    def _checkin(dbapi_conn, connection_record):
        ctx = _request_ctx.get()
        if ctx is None or not ctx.sampled:
            connection_record.info.pop("_perf_checkout_at", None)
            return
        started = connection_record.info.pop("_perf_checkout_at", None)
        if started is not None:
            # Tempo em que a conexão ficou emprestada (não a espera na fila do pool).
            ctx.pool_checkout_ms += (time.perf_counter() - started) * 1000.0

    _engine_listeners_attached = True


def pool_snapshot(engine: Any) -> dict[str, Any]:
    """Estatísticas não sensíveis do pool, quando disponíveis."""
    out: dict[str, Any] = {}
    try:
        pool = engine.pool
        size = getattr(pool, "size", None)
        checked_in = getattr(pool, "checkedin", None)
        checked_out = getattr(pool, "checkedout", None)
        overflow = getattr(pool, "overflow", None)
        if callable(size):
            out["pool_size"] = size()
        if callable(checked_in):
            out["pool_checked_in"] = checked_in()
        if callable(checked_out):
            out["pool_checked_out"] = checked_out()
        if callable(overflow):
            out["pool_overflow"] = overflow()
    except Exception:
        return out
    return out


class PerfInstrumentationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        if not PERF_INSTRUMENTATION_ENABLED:
            return await call_next(request)

        sampled = _should_sample()
        incoming = request.headers.get("x-request-id") or request.headers.get(
            "X-Request-Id"
        )
        request_id = (incoming or "").strip()[:64] or uuid.uuid4().hex[:16]
        path = _normalize_path(request.url.path)
        ctx = PerfRequestContext(
            request_id=request_id,
            method=request.method,
            path_template=path,
            sampled=sampled,
        )
        token = _request_ctx.set(ctx)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = getattr(response, "status_code", 500)
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            try:
                if sampled:
                    total_ms = (time.perf_counter() - ctx.started) * 1000.0
                    logger.info(
                        "perf_request request_id=%s method=%s path=%s status=%s "
                        "duration_ms=%.1f sql_count=%s sql_ms=%.1f "
                        "recalc_count=%s recalc_rows=%s "
                        "pool_checkouts=%s pool_held_ms=%.1f",
                        ctx.request_id,
                        ctx.method,
                        ctx.path_template,
                        status_code,
                        total_ms,
                        ctx.sql_count,
                        ctx.sql_ms,
                        ctx.recalc_count,
                        ctx.recalc_rows,
                        ctx.pool_checkouts,
                        ctx.pool_checkout_ms,
                    )
            except Exception:
                logger.exception("perf_request_log_failed")
            _request_ctx.reset(token)
