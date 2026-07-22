"""Encerramento automático de pendentes fora da janela de 2 quinzenas."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models import Saida, SaidaHistorico
from saida_operacional_utils import (
    carregar_contexto_operacional,
    deve_excluir_saida_operacional,
    timestamp_operacional_saida,
)
from saidas_routes import (
    STATUS_EM_ROTA,
    STATUS_ENCERRADO_SISTEMA,
    STATUS_SAIU_PARA_ENTREGA,
    normalizar_status_saida,
)

EVENTO_ENCERRADO = "encerrado_sistema"


def periodo_janela_viva_quinzenas(ref: date) -> Tuple[date, date]:
    """
    Retorna (inicio_vivo, fim_ref) = quinzena anterior + quinzena atual até ref.
    - ref.day <= 15: anterior = 16→fim mês anterior; atual = 1→ref
    - ref.day > 15: anterior = 1→15 do mês; atual = 16→ref
    """
    if ref.day <= 15:
        if ref.month == 1:
            ant_ano, ant_mes = ref.year - 1, 12
        else:
            ant_ano, ant_mes = ref.year, ref.month - 1
        inicio_anterior = date(ant_ano, ant_mes, 16)
        return inicio_anterior, ref
    return date(ref.year, ref.month, 1), ref


@dataclass
class EncerramentoResult:
    dry_run: bool
    ref_date: date
    inicio_vivo: date
    candidatos: int
    elegiveis: int
    atualizados: int
    por_sub_base: Dict[str, int]
    sample_ids: List[int]


def _chunked(ids: Sequence[int], size: int) -> List[List[int]]:
    return [list(ids[i : i + size]) for i in range(0, len(ids), size)]


def run_encerrar_pendentes_quinzena(
    db: Session,
    *,
    ref: Optional[date] = None,
    dry_run: bool = True,
    batch_size: int = 500,
    sub_base: Optional[str] = None,
) -> EncerramentoResult:
    ref_date = ref or date.today()
    inicio_vivo, _fim = periodo_janela_viva_quinzenas(ref_date)
    # Prefitro SQL amplo: evita varrer todo o histórico recente da janela viva.
    cutoff_ts = datetime.combine(inicio_vivo, datetime.min.time())

    q = select(Saida).where(
        Saida.codigo.isnot(None),
        or_(
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
            Saida.status == "saiu",
        ),
        or_(
            Saida.data.isnot(None) & (Saida.data < inicio_vivo),
            Saida.timestamp.isnot(None) & (Saida.timestamp < cutoff_ts),
            Saida.data.is_(None) & Saida.timestamp.is_(None),
        ),
    )
    if sub_base:
        q = q.where(Saida.sub_base == sub_base)

    candidates = list(db.scalars(q).all())
    por_sub_base: Dict[str, int] = {}
    sample_ids: List[int] = []
    elegiveis: List[Saida] = []

    for chunk in _chunked([int(s.id_saida) for s in candidates], max(50, min(batch_size, 250))):
        ctx_map = carregar_contexto_operacional(db, chunk)
        by_id = {int(s.id_saida): s for s in candidates if int(s.id_saida) in set(chunk)}
        for sid in chunk:
            s = by_id.get(sid)
            if not s:
                continue
            st = normalizar_status_saida(s.status)
            if st not in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA, "saiu"):
                continue
            ctx = ctx_map.get(sid)
            if deve_excluir_saida_operacional(ctx):
                continue
            ts_op = timestamp_operacional_saida(ctx, s.timestamp)
            data_op = ts_op.date() if ts_op else (s.data or None)
            if data_op is None or data_op >= inicio_vivo:
                continue
            elegiveis.append(s)

    atualizados = 0
    for s in elegiveis:
        sb = (s.sub_base or "").strip() or "_"
        por_sub_base[sb] = por_sub_base.get(sb, 0) + 1
        if len(sample_ids) < 20:
            sample_ids.append(int(s.id_saida))

    if not dry_run:
        for i in range(0, len(elegiveis), batch_size):
            batch = elegiveis[i : i + batch_size]
            for s in batch:
                status_anterior = s.status
                s.status = STATUS_ENCERRADO_SISTEMA
                db.add(
                    SaidaHistorico(
                        id_saida=s.id_saida,
                        evento=EVENTO_ENCERRADO,
                        status_anterior=status_anterior,
                        status_novo=STATUS_ENCERRADO_SISTEMA,
                        motoboy_id_anterior=s.motoboy_id,
                        motoboy_id_novo=s.motoboy_id,
                        user_id=None,
                        payload=f"inicio_vivo={inicio_vivo.isoformat()}",
                    )
                )
                atualizados += 1
            db.commit()

    return EncerramentoResult(
        dry_run=dry_run,
        ref_date=ref_date,
        inicio_vivo=inicio_vivo,
        candidatos=len(candidates),
        elegiveis=len(elegiveis),
        atualizados=atualizados if not dry_run else 0,
        por_sub_base=por_sub_base,
        sample_ids=sample_ids,
    )
