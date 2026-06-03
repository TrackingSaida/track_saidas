from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from models import HistoryRetentionPolicy, MaintenanceJobState, Saida, SaidaHistorico

logger = logging.getLogger("cleanup_service")

JOB_NAME = "history_cleanup_v1"
DEFAULT_RETENTION_DAYS = 60


@dataclass
class CleanupResult:
    status: str
    retention_days: int
    cutoff: datetime
    rows_historico: int
    rows_saidas: int
    processed_saida_ids: int
    last_saida_id: int
    duration_ms: int
    partial: bool
    error: str | None = None


def _utc_now() -> datetime:
    return datetime.utcnow()


def _load_retention_days(db: Session, fallback_days: int) -> int:
    policy = db.execute(
        select(HistoryRetentionPolicy)
        .where(HistoryRetentionPolicy.ativo.is_(True))
        .where(HistoryRetentionPolicy.sub_base == "__global__")
        .limit(1)
    ).scalar_one_or_none()
    if not policy:
        return fallback_days
    return max(1, int(policy.retention_days or fallback_days))


def _get_or_create_job_state(db: Session, retention_days: int) -> MaintenanceJobState:
    state = db.get(MaintenanceJobState, JOB_NAME)
    if state:
        if state.retention_days != retention_days:
            state.retention_days = retention_days
            db.commit()
            db.refresh(state)
        return state

    state = MaintenanceJobState(
        job_name=JOB_NAME,
        retention_days=retention_days,
        last_saida_id=0,
        status="idle",
    )
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def run_history_cleanup(
    db: Session,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = 3000,
    max_runtime_seconds: int = 540,
) -> CleanupResult:
    started = time.perf_counter()
    started_at = _utc_now()
    rows_historico = 0
    rows_saidas = 0
    processed_saida_ids = 0
    partial = False
    error_message: str | None = None

    retention_days = _load_retention_days(db, retention_days)
    cutoff = _utc_now() - timedelta(days=retention_days)
    state = _get_or_create_job_state(db, retention_days)

    state.status = "running"
    state.last_run_started_at = started_at
    state.last_error = None
    db.commit()

    last_saida_id = int(state.last_saida_id or 0)

    try:
        while True:
            elapsed = time.perf_counter() - started
            if elapsed >= max_runtime_seconds:
                partial = True
                break

            old_saida_ids = db.execute(
                select(Saida.id_saida)
                .where(Saida.timestamp < cutoff)
                .where(Saida.id_saida > last_saida_id)
                .order_by(Saida.id_saida.asc())
                .limit(batch_size)
            ).scalars().all()

            if not old_saida_ids:
                # Reinicia checkpoint quando não há mais registros acima do ponteiro.
                if last_saida_id > 0:
                    state.last_saida_id = 0
                    db.commit()
                break

            first_saida_id = int(old_saida_ids[0])
            current_last = int(old_saida_ids[-1])

            deleted_hist = db.execute(
                delete(SaidaHistorico).where(
                    SaidaHistorico.id_saida >= first_saida_id,
                    SaidaHistorico.id_saida <= current_last,
                    SaidaHistorico.id_saida.in_(old_saida_ids),
                )
            )
            deleted_saida = db.execute(
                delete(Saida).where(Saida.id_saida.in_(old_saida_ids))
            )
            db.commit()

            hist_count = int(deleted_hist.rowcount or 0)
            saida_count = int(deleted_saida.rowcount or 0)

            rows_historico += hist_count
            rows_saidas += saida_count
            processed_saida_ids += len(old_saida_ids)
            last_saida_id = current_last

            state.last_saida_id = last_saida_id
            state.last_rows_historico = rows_historico
            state.last_rows_saidas = rows_saidas
            state.updated_at = _utc_now()
            db.commit()

        status = "partial" if partial else "completed"
    except Exception as exc:
        db.rollback()
        status = "error"
        error_message = str(exc)
        logger.exception("history_cleanup_failed")
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        state.status = status
        state.last_rows_historico = rows_historico
        state.last_rows_saidas = rows_saidas
        state.last_duration_ms = duration_ms
        state.last_run_finished_at = _utc_now()
        state.last_error = error_message
        state.retention_days = retention_days
        state.updated_at = _utc_now()
        db.commit()

    return CleanupResult(
        status=status,
        retention_days=retention_days,
        cutoff=cutoff,
        rows_historico=rows_historico,
        rows_saidas=rows_saidas,
        processed_saida_ids=processed_saida_ids,
        last_saida_id=last_saida_id,
        duration_ms=state.last_duration_ms,
        partial=(status == "partial"),
        error=error_message,
    )


def estimate_old_volume(db: Session, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict[str, int]:
    retention_days = _load_retention_days(db, retention_days)
    cutoff = _utc_now() - timedelta(days=retention_days)
    old_saidas = db.execute(
        select(func.count()).select_from(Saida).where(Saida.timestamp < cutoff)
    ).scalar_one()
    old_historico = db.execute(
        select(func.count())
        .select_from(SaidaHistorico)
        .join(Saida, Saida.id_saida == SaidaHistorico.id_saida)
        .where(Saida.timestamp < cutoff)
    ).scalar_one()
    return {
        "retention_days": retention_days,
        "old_saidas": int(old_saidas or 0),
        "old_historico": int(old_historico or 0),
    }
