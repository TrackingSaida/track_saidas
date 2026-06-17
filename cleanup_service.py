from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import os
import time
from typing import Any

from sqlalchemy import delete, exists, func, inspect as sa_inspect, select, text
from sqlalchemy.exc import ProgrammingError, OperationalError
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

OPTIONAL_TABLES = frozenset({
    "saida_historico",
    "saidas_detail",
    "owner_cobranca_itens",
    "logs_leitura",
    "rotas_motoboy",
    "address_telemetry",
    "geocode_cache",
    "suggestion_cache",
    "enderecos_conhecidos",
    "coletas",
})

CRITICAL_TABLES = frozenset({
    "saidas",
    "maintenance_job_state",
    "history_retention_policy",
})


@dataclass
class _CleanupContext:
    skipped_tables: set[str] = field(default_factory=set)
    _exists_cache: dict[str, bool] = field(default_factory=dict)

    def skipped_list(self) -> list[str]:
        return sorted(self.skipped_tables)

    def table_exists(self, db: Session, table_name: str) -> bool:
        if table_name in self._exists_cache:
            return self._exists_cache[table_name]

        exists_flag = False
        try:
            bind = db.get_bind()
            exists_flag = sa_inspect(bind).has_table(table_name, schema="public")
        except Exception:
            exists_flag = False

        if not exists_flag:
            regclass = db.execute(
                text("SELECT to_regclass(:name)"),
                {"name": f"public.{table_name}"},
            ).scalar_one()
            exists_flag = regclass is not None

        self._exists_cache[table_name] = exists_flag
        return exists_flag

    def mark_skipped(self, table_name: str) -> None:
        if table_name not in self.skipped_tables:
            self.skipped_tables.add(table_name)
            logger.warning("cleanup_table_skipped table=%s reason=missing", table_name)

    def require_table(self, db: Session, table_name: str) -> bool:
        if self.table_exists(db, table_name):
            return True
        if table_name in OPTIONAL_TABLES:
            self.mark_skipped(table_name)
            return False
        if table_name in CRITICAL_TABLES:
            raise RuntimeError(f"Tabela crítica ausente: {table_name}")
        raise RuntimeError(f"Tabela ausente: {table_name}")


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
    skipped_tables: list[str] = field(default_factory=list)
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


def _safe_scalar_count(ctx: _CleanupContext, db: Session, table_name: str, stmt) -> int:
    if not ctx.require_table(db, table_name):
        return 0
    try:
        return int(db.execute(stmt).scalar_one() or 0)
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        ctx.mark_skipped(table_name)
        logger.warning("cleanup_count_failed table=%s error=%s", table_name, exc)
        return 0


def _safe_execute_delete(ctx: _CleanupContext, db: Session, table_name: str, delete_stmt) -> int:
    if not ctx.require_table(db, table_name):
        return 0
    try:
        result = db.execute(delete_stmt)
        return int(result.rowcount or 0)
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        ctx.mark_skipped(table_name)
        logger.warning("cleanup_delete_failed table=%s error=%s", table_name, exc)
        return 0


def _safe_select_ids(
    ctx: _CleanupContext,
    db: Session,
    table_name: str,
    stmt,
) -> list[Any]:
    if not ctx.require_table(db, table_name):
        return []
    try:
        return list(db.execute(stmt).scalars().all())
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        ctx.mark_skipped(table_name)
        logger.warning("cleanup_select_failed table=%s error=%s", table_name, exc)
        return []


def _safe_delete_by_ids(
    ctx: _CleanupContext,
    db: Session,
    table_name: str,
    model,
    id_column,
    ids: list,
) -> int:
    if not ids:
        return 0
    return _safe_execute_delete(ctx, db, table_name, delete(model).where(id_column.in_(ids)))


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


def _run_phase_b(
    db: Session,
    ctx: _CleanupContext,
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
            deleted = step(
                db,
                ctx,
                cutoff=cutoff,
                cutoff_date=cutoff_date,
                batch_size=batch_size,
                counters=counters,
            )
            if deleted > 0:
                db.commit()
                progress = True
        if not progress:
            break
    return False


def _phase_b_logs_leitura(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "logs_leitura",
        select(LogLeitura.id)
        .where(LogLeitura.created_at < cutoff)
        .order_by(LogLeitura.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "logs_leitura", LogLeitura, LogLeitura.id, ids)
    counters.rows_logs_leitura += count
    return count


def _phase_b_rotas_motoboy(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "rotas_motoboy",
        select(RotasMotoboy.id)
        .where(RotasMotoboy.data < cutoff_date)
        .where(RotasMotoboy.status.in_(("finalizada", "cancelada")))
        .order_by(RotasMotoboy.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "rotas_motoboy", RotasMotoboy, RotasMotoboy.id, ids)
    counters.rows_rotas += count
    return count


def _phase_b_address_telemetry(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "address_telemetry",
        select(AddressTelemetry.id)
        .where(AddressTelemetry.created_at < cutoff)
        .order_by(AddressTelemetry.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "address_telemetry", AddressTelemetry, AddressTelemetry.id, ids)
    counters.rows_address_telemetry += count
    return count


def _phase_b_geocode_cache(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "geocode_cache",
        select(GeocodeCache.id)
        .where(GeocodeCache.updated_at < cutoff)
        .order_by(GeocodeCache.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "geocode_cache", GeocodeCache, GeocodeCache.id, ids)
    counters.rows_geocode_cache += count
    return count


def _phase_b_suggestion_cache(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "suggestion_cache",
        select(SuggestionCache.id)
        .where(SuggestionCache.updated_at < cutoff)
        .order_by(SuggestionCache.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "suggestion_cache", SuggestionCache, SuggestionCache.id, ids)
    counters.rows_suggestion_cache += count
    return count


def _phase_b_enderecos_conhecidos(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "enderecos_conhecidos",
        select(EnderecoConhecido.id)
        .where(EnderecoConhecido.ultima_utilizacao < cutoff)
        .order_by(EnderecoConhecido.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "enderecos_conhecidos", EnderecoConhecido, EnderecoConhecido.id, ids)
    counters.rows_enderecos_conhecidos += count
    return count


def _phase_b_orphan_details(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    if not ctx.require_table(db, "saidas_detail"):
        return 0

    ids = _safe_select_ids(
        ctx,
        db,
        "saidas_detail",
        select(SaidaDetail.id_detail)
        .where(SaidaDetail.timestamp < cutoff)
        .where(~exists(select(Saida.id_saida).where(Saida.id_saida == SaidaDetail.id_saida)))
        .order_by(SaidaDetail.id_detail.asc())
        .limit(batch_size),
    )
    if not ids:
        return 0

    foto_urls = _safe_select_ids(
        ctx,
        db,
        "saidas_detail",
        select(SaidaDetail.foto_url).where(SaidaDetail.id_detail.in_(ids)),
    )
    batch_b2_deleted, batch_b2_failed = _purge_detail_photos(list(foto_urls))
    counters.b2_objects_deleted += batch_b2_deleted
    counters.b2_objects_failed += batch_b2_failed
    count = _safe_delete_by_ids(ctx, db, "saidas_detail", SaidaDetail, SaidaDetail.id_detail, ids)
    counters.rows_detail_orphans += count
    return count


def _phase_b_orphan_cobranca(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    ids = _safe_select_ids(
        ctx,
        db,
        "owner_cobranca_itens",
        select(OwnerCobrancaItem.id)
        .where(OwnerCobrancaItem.timestamp < cutoff)
        .where(OwnerCobrancaItem.id_saida.isnot(None))
        .where(~exists(select(Saida.id_saida).where(Saida.id_saida == OwnerCobrancaItem.id_saida)))
        .order_by(OwnerCobrancaItem.id.asc())
        .limit(batch_size),
    )
    count = _safe_delete_by_ids(ctx, db, "owner_cobranca_itens", OwnerCobrancaItem, OwnerCobrancaItem.id, ids)
    counters.rows_cobranca_orphans += count
    return count


def _phase_b_orphan_coletas(db, ctx, *, cutoff, cutoff_date, batch_size, counters) -> int:
    if not ctx.require_table(db, "coletas"):
        return 0

    ids = _safe_select_ids(
        ctx,
        db,
        "coletas",
        select(Coleta.id_coleta)
        .where(Coleta.timestamp < cutoff)
        .where(~exists(select(Saida.id_saida).where(Saida.id_coleta == Coleta.id_coleta)))
        .order_by(Coleta.id_coleta.asc())
        .limit(batch_size),
    )
    if not ids:
        return 0

    if ctx.require_table(db, "owner_cobranca_itens"):
        _safe_execute_delete(
            ctx,
            db,
            "owner_cobranca_itens",
            delete(OwnerCobrancaItem).where(OwnerCobrancaItem.id_coleta.in_(ids)),
        )

    count = _safe_execute_delete(
        ctx,
        db,
        "coletas",
        delete(Coleta).where(Coleta.id_coleta.in_(ids)),
    )
    counters.rows_coletas += count
    return count


def run_history_cleanup(
    db: Session,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = 3000,
    max_runtime_seconds: int = 540,
    ctx: _CleanupContext | None = None,
) -> CleanupResult:
    ctx = ctx or _CleanupContext()
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
        ctx.require_table(db, "saidas")

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

            if ctx.require_table(db, "saidas_detail"):
                foto_urls = db.execute(
                    select(SaidaDetail.foto_url).where(SaidaDetail.id_saida.in_(old_saida_ids))
                ).scalars().all()
                batch_b2_deleted, batch_b2_failed = _purge_detail_photos(list(foto_urls))
                b2_deleted += batch_b2_deleted
                b2_failed += batch_b2_failed

            rows_historico += _safe_execute_delete(
                ctx,
                db,
                "saida_historico",
                delete(SaidaHistorico).where(
                    SaidaHistorico.id_saida >= first_saida_id,
                    SaidaHistorico.id_saida <= current_last,
                    SaidaHistorico.id_saida.in_(old_saida_ids),
                ),
            )
            rows_detail += _safe_execute_delete(
                ctx,
                db,
                "saidas_detail",
                delete(SaidaDetail).where(SaidaDetail.id_saida.in_(old_saida_ids)),
            )
            rows_cobranca += _safe_execute_delete(
                ctx,
                db,
                "owner_cobranca_itens",
                delete(OwnerCobrancaItem).where(OwnerCobrancaItem.id_saida.in_(old_saida_ids)),
            )
            rows_logs_leitura += _safe_execute_delete(
                ctx,
                db,
                "logs_leitura",
                delete(LogLeitura).where(LogLeitura.id_saida.in_(old_saida_ids)),
            )

            deleted_saida = db.execute(delete(Saida).where(Saida.id_saida.in_(old_saida_ids)))
            db.commit()

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
                ctx,
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
        skipped_tables=ctx.skipped_list(),
        error=error_message,
    )


def estimate_old_volume(
    db: Session,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    ctx: _CleanupContext | None = None,
) -> dict[str, int]:
    ctx = ctx or _CleanupContext()
    retention_days = _load_retention_days(db, retention_days)
    cutoff = _utc_now() - timedelta(days=retention_days)
    cutoff_date = cutoff.date()

    old_saidas = db.execute(
        select(func.count()).select_from(Saida).where(Saida.timestamp < cutoff)
    ).scalar_one()

    return {
        "retention_days": retention_days,
        "old_saidas": int(old_saidas or 0),
        "old_saida_historico": _safe_scalar_count(
            ctx,
            db,
            "saida_historico",
            select(func.count())
            .select_from(SaidaHistorico)
            .join(Saida, Saida.id_saida == SaidaHistorico.id_saida)
            .where(Saida.timestamp < cutoff),
        ),
        "old_saidas_detail": _safe_scalar_count(
            ctx,
            db,
            "saidas_detail",
            select(func.count())
            .select_from(SaidaDetail)
            .join(Saida, Saida.id_saida == SaidaDetail.id_saida)
            .where(Saida.timestamp < cutoff),
        ),
        "orphan_saidas_detail": _safe_scalar_count(
            ctx,
            db,
            "saidas_detail",
            select(func.count())
            .select_from(SaidaDetail)
            .where(~exists(select(Saida.id_saida).where(Saida.id_saida == SaidaDetail.id_saida))),
        ),
        "old_logs_leitura": _safe_scalar_count(
            ctx,
            db,
            "logs_leitura",
            select(func.count()).select_from(LogLeitura).where(LogLeitura.created_at < cutoff),
        ),
        "old_rotas_motoboy": _safe_scalar_count(
            ctx,
            db,
            "rotas_motoboy",
            select(func.count())
            .select_from(RotasMotoboy)
            .where(RotasMotoboy.data < cutoff_date)
            .where(RotasMotoboy.status.in_(("finalizada", "cancelada"))),
        ),
        "old_coletas_orfas": _safe_scalar_count(
            ctx,
            db,
            "coletas",
            select(func.count())
            .select_from(Coleta)
            .where(Coleta.timestamp < cutoff)
            .where(~exists(select(Saida.id_saida).where(Saida.id_coleta == Coleta.id_coleta))),
        ),
        "old_address_telemetry": _safe_scalar_count(
            ctx,
            db,
            "address_telemetry",
            select(func.count()).select_from(AddressTelemetry).where(AddressTelemetry.created_at < cutoff),
        ),
        "old_geocode_cache": _safe_scalar_count(
            ctx,
            db,
            "geocode_cache",
            select(func.count()).select_from(GeocodeCache).where(GeocodeCache.updated_at < cutoff),
        ),
        "old_suggestion_cache": _safe_scalar_count(
            ctx,
            db,
            "suggestion_cache",
            select(func.count()).select_from(SuggestionCache).where(SuggestionCache.updated_at < cutoff),
        ),
        "old_enderecos_conhecidos": _safe_scalar_count(
            ctx,
            db,
            "enderecos_conhecidos",
            select(func.count())
            .select_from(EnderecoConhecido)
            .where(EnderecoConhecido.ultima_utilizacao < cutoff),
        ),
    }
