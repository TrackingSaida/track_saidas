"""Encerramento automático de pendentes fora da janela de 2 quinzenas."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from models import Saida, SaidaHistorico
from saida_operacional_utils import (
    carregar_contexto_operacional,
    deve_excluir_saida_operacional,
    timestamp_operacional_saida,
)

STATUS_SAIU_PARA_ENTREGA = "SAIU_PARA_ENTREGA"
STATUS_EM_ROTA = "EM_ROTA"
STATUS_ENCERRADO_SISTEMA = "ENCERRADO_SISTEMA"
EVENTO_ENCERRADO = "encerrado_sistema"
# Chunk só para carregar contexto operacional (não é o limite de escrita).
_CTX_CHUNK = 250


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
    restantes: int
    tem_mais: bool
    por_sub_base: Dict[str, int]
    sample_ids: List[int]
    tempo_execucao_ms: int
    batch_size: int


def _chunked(ids: Sequence[int], size: int) -> List[List[int]]:
    return [list(ids[i : i + size]) for i in range(0, len(ids), size)]


def _status_encerravel(status: Optional[str]) -> bool:
    s = (status or "").strip().upper().replace(" ", "_")
    return s in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA, "SAIU")


def run_encerrar_pendentes_quinzena(
    db: Session,
    *,
    ref: Optional[date] = None,
    dry_run: bool = True,
    batch_size: int = 500,
    sub_base: Optional[str] = None,
) -> EncerramentoResult:
    """
    Encerra no máximo ``batch_size`` elegíveis por chamada (limite por requisição).

    - dry_run=True: conta elegíveis, não altera nada.
    - dry_run=False: atualiza até ``batch_size`` registros e faz **um** commit.
    - Usa UPDATE via Core (não ORM) para não disparar ``saida_after_update``
      (que recalcula coleta a cada alteração de Saida).
    """
    started = time.perf_counter()
    limit = max(1, int(batch_size))
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
    candidatos = len(candidates)

    selecionados: List[Saida] = []
    total_elegiveis = 0
    por_sub_base: Dict[str, int] = {}
    sample_ids: List[int] = []

    for chunk in _chunked([int(s.id_saida) for s in candidates], _CTX_CHUNK):
        ctx_map = carregar_contexto_operacional(db, chunk)
        by_id = {int(s.id_saida): s for s in candidates if int(s.id_saida) in set(chunk)}
        for sid in chunk:
            s = by_id.get(sid)
            if not s:
                continue
            if not _status_encerravel(s.status):
                continue
            ctx = ctx_map.get(sid)
            if deve_excluir_saida_operacional(ctx):
                continue
            ts_op = timestamp_operacional_saida(ctx, s.timestamp)
            data_op = ts_op.date() if ts_op else (s.data or None)
            if data_op is None or data_op >= inicio_vivo:
                continue

            total_elegiveis += 1
            if len(selecionados) >= limit:
                # Já temos o lote desta requisição; só continua contando elegíveis.
                continue

            selecionados.append(s)
            sb = (s.sub_base or "").strip() or "_"
            por_sub_base[sb] = por_sub_base.get(sb, 0) + 1
            if len(sample_ids) < 20:
                sample_ids.append(int(s.id_saida))

    atualizados = 0
    if not dry_run and selecionados:
        # UPDATE Core: não dispara models.saida_after_update (recalcular_coleta).
        ids = [int(s.id_saida) for s in selecionados]
        status_por_id = {int(s.id_saida): s.status for s in selecionados}
        motoboy_por_id = {int(s.id_saida): s.motoboy_id for s in selecionados}

        db.execute(
            update(Saida)
            .where(Saida.id_saida.in_(ids))
            .values(status=STATUS_ENCERRADO_SISTEMA)
        )
        payload = f"inicio_vivo={inicio_vivo.isoformat()}"
        for sid in ids:
            db.add(
                SaidaHistorico(
                    id_saida=sid,
                    evento=EVENTO_ENCERRADO,
                    status_anterior=status_por_id.get(sid),
                    status_novo=STATUS_ENCERRADO_SISTEMA,
                    motoboy_id_anterior=motoboy_por_id.get(sid),
                    motoboy_id_novo=motoboy_por_id.get(sid),
                    user_id=None,
                    payload=payload,
                )
            )
        db.commit()
        atualizados = len(ids)
        # Evita objetos ORM stale com status antigo na mesma sessão.
        db.expire_all()

    restantes = max(0, total_elegiveis - atualizados)
    tempo_ms = int((time.perf_counter() - started) * 1000)

    return EncerramentoResult(
        dry_run=dry_run,
        ref_date=ref_date,
        inicio_vivo=inicio_vivo,
        candidatos=candidatos,
        elegiveis=total_elegiveis,
        atualizados=atualizados,
        restantes=restantes,
        tem_mais=restantes > 0,
        por_sub_base=por_sub_base,
        sample_ids=sample_ids,
        tempo_execucao_ms=tempo_ms,
        batch_size=limit,
    )
