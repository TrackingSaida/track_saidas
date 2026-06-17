from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
import time

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from models import (
    AddressTelemetry,
    Coleta,
    EnderecoConhecido,
    GeocodeCache,
    HistoryRetentionPolicy,
    LogLeitura,
    MaintenanceJobState,
    OwnerCobrancaItem,
    RotasMotoboy,
    Saida,
    SaidaDetail,
    SaidaHistorico,
    SuggestionCache,
)
from upload_storage_utils import collect_b2_keys_from_foto_urls, purge_b2_keys

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
    rows_detail: int = 0
    rows_cobranca: int = 0
    rows_logs_leitura: int = 0
    rows_coletas: int = 0
    rows_rotas: int = 0
    rows_address_telemetry: int = 0
    rows_geocode_cache: int = 0
    rows_suggestion_cache: int = 0
    rows_enderecos_conhecidos: int = 0
    b2_objects_deleted: int = 0
    b2_objects_failed: int = 0
    processed_saida_ids: int = 0
    last_saida_id: int = 0
    duration_ms: int = 0
    partial: bool = False
    error: str | None = None


@dataclass
class _PhaseBCounters:
    rows_logs_leitura: int = 0
    rows_coletas: int = 0
    rows_rotas: int = 0
    rows_address_telemetry: int = 0
    rows_geocode_cache: int = 0
    rows_suggestion_cache: int = 0
    rows_enderecos_conhecidos: int = 0
    rows_detail_orphans: int = 0
    rows_cobranca_orphans: int = 0
    b2_objects_deleted: int = 0
    b2_objects_failed: int = 0


def _utc_now() -> datetime:
    return datetime.utcnow()


def _b2_purge_enabled() -> bool:
    raw = os.getenv("HISTORY_CLEANUP_B2_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


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


def _purge_detail_photos(foto_urls: list[str | None]) -> tuple[int, int]:
    if not _b2_purge_enabled():
        return 0, 0
    keys = collect_b2_keys_from_foto_urls(foto_urls)
    return purge_b2_keys(keys)


def _delete_ids_subquery(db: Session, model, id_column, ids: list) -> int:
    if not ids:
        return 0
    result = db.execute(delete(model).where(id_column.in_(ids)))
    return int(result.rowcount or 0)


def _run_phase_b(
    db: Session,
    *,
    cutoff: datetime,
    batch_size: int,
    deadline: float,
    counters: _PhaseBCounters,
) -> bool:
    """Limpeza por data / órfãos. Retorna True se parou por falta de tempo."""
    cutoff_date = cutoff.date()
    steps = (
        _phase_b_logs_leitura,
        _phase_b_rotas_motoboy,
        _phase_b_address_telemetry,
        _phase_b_geocode_cache,
        _phase_b_suggestion_cache,
        _phase_b_enderecos_conhecidos,
        _phase_b_orphan_details,
        _phase_b_orphan_cobranca,
        _phase_b_orphan_coletas,
    )

    while True:
        if time.perf_counter() >= deadline:
            return True

        progress = False
        for step in steps:
            if time.perf_counter() >= deadline:
                return True
            deleted = step(db, cutoff=cutoff, cutoff_date=cutoff_date, batch_size=batch_size, counters=counters)
            if deleted > 0:
                db.commit()
                progress = True
        if not progress:
            break
    return False


def _phase_b_logs_leitura(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(LogLeitura.id)
        .where(LogLeitura.created_at < cutoff)
        .order_by(LogLeitura.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, LogLeitura, LogLeitura.id, ids)
    counters.rows_logs_leitura += count
    return count


def _phase_b_rotas_motoboy(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(RotasMotoboy.id)
        .where(RotasMotoboy.data < cutoff_date)
        .where(RotasMotoboy.status.in_(("finalizada", "cancelada")))
        .order_by(RotasMotoboy.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, RotasMotoboy, RotasMotoboy.id, ids)
    counters.rows_rotas += count
    return count


def _phase_b_address_telemetry(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(AddressTelemetry.id)
        .where(AddressTelemetry.created_at < cutoff)
        .order_by(AddressTelemetry.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, AddressTelemetry, AddressTelemetry.id, ids)
    counters.rows_address_telemetry += count
    return count


def _phase_b_geocode_cache(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(GeocodeCache.id)
        .where(GeocodeCache.updated_at < cutoff)
        .order_by(GeocodeCache.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, GeocodeCache, GeocodeCache.id, ids)
    counters.rows_geocode_cache += count
    return count


def _phase_b_suggestion_cache(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(SuggestionCache.id)
        .where(SuggestionCache.updated_at < cutoff)
        .order_by(SuggestionCache.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, SuggestionCache, SuggestionCache.id, ids)
    counters.rows_suggestion_cache += count
    return count


def _phase_b_enderecos_conhecidos(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(EnderecoConhecido.id)
        .where(EnderecoConhecido.ultima_utilizacao < cutoff)
        .order_by(EnderecoConhecido.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, EnderecoConhecido, EnderecoConhecido.id, ids)
    counters.rows_enderecos_conhecidos += count
    return count


def _phase_b_orphan_details(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(SaidaDetail.id_detail)
        .where(SaidaDetail.timestamp < cutoff)
        .where(~exists(select(Saida.id_saida).where(Saida.id_saida == SaidaDetail.id_saida)))
        .order_by(SaidaDetail.id_detail.asc())
        .limit(batch_size)
    ).scalars().all()
    if not ids:
        return 0

    foto_urls = db.execute(
        select(SaidaDetail.foto_url).where(SaidaDetail.id_detail.in_(ids))
    ).scalars().all()
    batch_b2_deleted, batch_b2_failed = _purge_detail_photos(list(foto_urls))
    counters.b2_objects_deleted += batch_b2_deleted
    counters.b2_objects_failed += batch_b2_failed
    counters.rows_detail_orphans += len(ids)
    db.execute(delete(SaidaDetail).where(SaidaDetail.id_detail.in_(ids)))
    return len(ids)


def _phase_b_orphan_cobranca(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(OwnerCobrancaItem.id)
        .where(OwnerCobrancaItem.timestamp < cutoff)
        .where(OwnerCobrancaItem.id_saida.isnot(None))
        .where(~exists(select(Saida.id_saida).where(Saida.id_saida == OwnerCobrancaItem.id_saida)))
        .order_by(OwnerCobrancaItem.id.asc())
        .limit(batch_size)
    ).scalars().all()
    count = _delete_ids_subquery(db, OwnerCobrancaItem, OwnerCobrancaItem.id, ids)
    counters.rows_cobranca_orphans += count
    return count


def _phase_b_orphan_coletas(db, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = db.execute(
        select(Coleta.id_coleta)
        .where(Coleta.timestamp < cutoff)
        .where(~exists(select(Saida.id_saida).where(Saida.id_coleta == Coleta.id_coleta)))
        .order_by(Coleta.id_coleta.asc())
        .limit(batch_size)
    ).scalars().all()
    if not ids:
        return 0

    db.execute(delete(OwnerCobrancaItem).where(OwnerCobrancaItem.id_coleta.in_(ids)))
    result = db.execute(delete(Coleta).where(Coleta.id_coleta.in_(ids)))
    count = int(result.rowcount or 0)
    counters.rows_coletas += count
    return count


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
    rows_detail = 0
    rows_cobranca = 0
    rows_logs_leitura = 0
    b2_deleted = 0
    b2_failed = 0
    processed_saida_ids = 0
    partial = False
    error_message: str | None = None
    phase_b = _PhaseBCounters()

    retention_days = _load_retention_days(db, retention_days)
    cutoff = _utc_now() - timedelta(days=retention_days)
    state = _get_or_create_job_state(db, retention_days)
    deadline = started + max_runtime_seconds

    state.status = "running"
    state.last_run_started_at = started_at
    state.last_error = None
    db.commit()

    last_saida_id = int(state.last_saida_id or 0)

    try:
        while True:
            if time.perf_counter() >= deadline:
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
                if last_saida_id > 0:
                    state.last_saida_id = 0
                    db.commit()
                break

            first_saida_id = int(old_saida_ids[0])
            current_last = int(old_saida_ids[-1])

            foto_urls = db.execute(
                select(SaidaDetail.foto_url).where(SaidaDetail.id_saida.in_(old_saida_ids))
            ).scalars().all()
            batch_b2_deleted, batch_b2_failed = _purge_detail_photos(list(foto_urls))
            b2_deleted += batch_b2_deleted
            b2_failed += batch_b2_failed

            deleted_hist = db.execute(
                delete(SaidaHistorico).where(
                    SaidaHistorico.id_saida >= first_saida_id,
                    SaidaHistorico.id_saida <= current_last,
                    SaidaHistorico.id_saida.in_(old_saida_ids),
                )
            )
            deleted_detail = db.execute(
                delete(SaidaDetail).where(SaidaDetail.id_saida.in_(old_saida_ids))
            )
            deleted_cobranca = db.execute(
                delete(OwnerCobrancaItem).where(OwnerCobrancaItem.id_saida.in_(old_saida_ids))
            )
            deleted_logs = db.execute(
                delete(LogLeitura).where(LogLeitura.id_saida.in_(old_saida_ids))
            )
            deleted_saida = db.execute(
                delete(Saida).where(Saida.id_saida.in_(old_saida_ids))
            )
            db.commit()

            rows_historico += int(deleted_hist.rowcount or 0)
            rows_detail += int(deleted_detail.rowcount or 0)
            rows_cobranca += int(deleted_cobranca.rowcount or 0)
            rows_logs_leitura += int(deleted_logs.rowcount or 0)
            rows_saidas += int(deleted_saida.rowcount or 0)
            processed_saida_ids += len(old_saida_ids)
            last_saida_id = current_last

            state.last_saida_id = last_saida_id
            state.last_rows_historico = rows_historico
            state.last_rows_saidas = rows_saidas
            state.updated_at = _utc_now()
            db.commit()

        if time.perf_counter() < deadline:
            phase_b_partial = _run_phase_b(
                db,
                cutoff=cutoff,
                batch_size=batch_size,
                deadline=deadline,
                counters=phase_b,
            )
            if phase_b_partial:
                partial = True
            db.commit()

        rows_detail += phase_b.rows_detail_orphans
        rows_cobranca += phase_b.rows_cobranca_orphans
        rows_logs_leitura += phase_b.rows_logs_leitura
        b2_deleted += phase_b.b2_objects_deleted
        b2_failed += phase_b.b2_objects_failed

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
        rows_detail=rows_detail,
        rows_cobranca=rows_cobranca,
        rows_logs_leitura=rows_logs_leitura,
        rows_coletas=phase_b.rows_coletas,
        rows_rotas=phase_b.rows_rotas,
        rows_address_telemetry=phase_b.rows_address_telemetry,
        rows_geocode_cache=phase_b.rows_geocode_cache,
        rows_suggestion_cache=phase_b.rows_suggestion_cache,
        rows_enderecos_conhecidos=phase_b.rows_enderecos_conhecidos,
        b2_objects_deleted=b2_deleted,
        b2_objects_failed=b2_failed,
        processed_saida_ids=processed_saida_ids,
        last_saida_id=last_saida_id,
        duration_ms=state.last_duration_ms,
        partial=(status == "partial"),
        error=error_message,
    )


def estimate_old_volume(db: Session, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> dict[str, int]:
    retention_days = _load_retention_days(db, retention_days)
    cutoff = _utc_now() - timedelta(days=retention_days)
    cutoff_date = cutoff.date()

    old_saidas = db.execute(
        select(func.count()).select_from(Saida).where(Saida.timestamp < cutoff)
    ).scalar_one()
    old_historico = db.execute(
        select(func.count())
        .select_from(SaidaHistorico)
        .join(Saida, Saida.id_saida == SaidaHistorico.id_saida)
        .where(Saida.timestamp < cutoff)
    ).scalar_one()
    old_detail = db.execute(
        select(func.count())
        .select_from(SaidaDetail)
        .join(Saida, Saida.id_saida == SaidaDetail.id_saida)
        .where(Saida.timestamp < cutoff)
    ).scalar_one()
    orphan_detail = db.execute(
        select(func.count())
        .select_from(SaidaDetail)
        .where(~exists(select(Saida.id_saida).where(Saida.id_saida == SaidaDetail.id_saida)))
    ).scalar_one()
    old_logs = db.execute(
        select(func.count()).select_from(LogLeitura).where(LogLeitura.created_at < cutoff)
    ).scalar_one()
    old_rotas = db.execute(
        select(func.count())
        .select_from(RotasMotoboy)
        .where(RotasMotoboy.data < cutoff_date)
        .where(RotasMotoboy.status.in_(("finalizada", "cancelada")))
    ).scalar_one()
    old_coletas = db.execute(
        select(func.count())
        .select_from(Coleta)
        .where(Coleta.timestamp < cutoff)
        .where(~exists(select(Saida.id_saida).where(Saida.id_coleta == Coleta.id_coleta)))
    ).scalar_one()

    return {
        "retention_days": retention_days,
        "old_saidas": int(old_saidas or 0),
        "old_saida_historico": int(old_historico or 0),
        "old_saidas_detail": int(old_detail or 0),
        "orphan_saidas_detail": int(orphan_detail or 0),
        "old_logs_leitura": int(old_logs or 0),
        "old_rotas_motoboy": int(old_rotas or 0),
        "old_coletas_orfas": int(old_coletas or 0),
        "old_address_telemetry": int(
            db.execute(
                select(func.count()).select_from(AddressTelemetry).where(AddressTelemetry.created_at < cutoff)
            ).scalar_one()
            or 0
        ),
        "old_geocode_cache": int(
            db.execute(
                select(func.count()).select_from(GeocodeCache).where(GeocodeCache.updated_at < cutoff)
            ).scalar_one()
            or 0
        ),
        "old_suggestion_cache": int(
            db.execute(
                select(func.count()).select_from(SuggestionCache).where(SuggestionCache.updated_at < cutoff)
            ).scalar_one()
            or 0
        ),
        "old_enderecos_conhecidos": int(
            db.execute(
                select(func.count())
                .select_from(EnderecoConhecido)
                .where(EnderecoConhecido.ultima_utilizacao < cutoff)
            ).scalar_one()
            or 0
        ),
    }
