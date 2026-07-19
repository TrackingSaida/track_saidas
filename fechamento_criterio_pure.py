"""Regras puras de elegibilidade do fechamento (sem SQLAlchemy)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, List, Optional, Sequence

from saida_operacional_pure import EVENTOS_ATRIBUICAO_VALIDOS, EVENTOS_REATRIBUICAO

MODO_OPERACIONAL = "operacional"
MODO_CONFIRMACAO_ENTREGA = "confirmacao_entrega"
MODOS_VALIDOS = {MODO_OPERACIONAL, MODO_CONFIRMACAO_ENTREGA}

EVENTOS_ENTREGA = {"entregue", "entregue_lote"}
EVENTOS_AUSENCIA = {"ausente", "ausente_lote"}


@dataclass(frozen=True)
class HistoricoEventoLite:
    id: Optional[int]
    evento: str
    timestamp: Optional[datetime]
    motoboy_id_anterior: Optional[int] = None
    motoboy_id_novo: Optional[int] = None


@dataclass(frozen=True)
class EntregaEfetiva:
    id_saida: int
    motoboy_id: Optional[int]
    data_confirmacao: date
    ts_confirmacao: datetime
    id_historico_confirmacao: Optional[int]
    id_historico_atribuicao: Optional[int]
    data_operacional: Optional[date]
    reaberta: bool = False


def normalizar_modo(modo: Optional[str]) -> str:
    m = (modo or MODO_OPERACIONAL).strip().lower()
    if m not in MODOS_VALIDOS:
        return MODO_OPERACIONAL
    return m


def _norm_evento(evento: Optional[str]) -> str:
    return (evento or "").strip().lower().replace(" ", "_")


def resolver_entrega_efetiva(
    id_saida: int,
    eventos: Sequence[HistoricoEventoLite],
    motoboy_atual: Optional[int] = None,
) -> Optional[EntregaEfetiva]:
    """
    Resolve a última entrega efetiva de uma saída.

    Uma entrega seguida de reatribuição deixa de ser elegível até nova entrega.
    O motoboy remunerado é o da última atribuição válida anterior à entrega.
    """
    ordenados = sorted(
        [e for e in eventos if e.timestamp is not None],
        key=lambda e: (e.timestamp, e.id or 0),
    )
    if not ordenados:
        return None

    motoboy_atribuido: Optional[int] = None
    id_hist_atribuicao: Optional[int] = None
    ts_operacional: Optional[datetime] = None

    ultima_entrega: Optional[EntregaEfetiva] = None
    reaberta = False

    for ev in ordenados:
        key = _norm_evento(ev.evento)
        if key in EVENTOS_ATRIBUICAO_VALIDOS or key in EVENTOS_REATRIBUICAO:
            if ev.motoboy_id_novo is not None:
                motoboy_atribuido = int(ev.motoboy_id_novo)
            id_hist_atribuicao = ev.id
            ts_operacional = ev.timestamp
            if key in EVENTOS_REATRIBUICAO and ultima_entrega is not None:
                # Reatribuição após entrega reabre o pacote e invalida a entrega anterior.
                ultima_entrega = None
                reaberta = True
            continue

        if key in EVENTOS_ENTREGA:
            motoboy_pago = motoboy_atribuido
            if motoboy_pago is None and motoboy_atual is not None:
                motoboy_pago = int(motoboy_atual)
            ultima_entrega = EntregaEfetiva(
                id_saida=id_saida,
                motoboy_id=motoboy_pago,
                data_confirmacao=ev.timestamp.date(),
                ts_confirmacao=ev.timestamp,
                id_historico_confirmacao=ev.id,
                id_historico_atribuicao=id_hist_atribuicao,
                data_operacional=ts_operacional.date() if ts_operacional else None,
                reaberta=False,
            )
            reaberta = False

    if ultima_entrega is None:
        if reaberta:
            return EntregaEfetiva(
                id_saida=id_saida,
                motoboy_id=motoboy_atribuido or motoboy_atual,
                data_confirmacao=date.min,
                ts_confirmacao=datetime.min,
                id_historico_confirmacao=None,
                id_historico_atribuicao=id_hist_atribuicao,
                data_operacional=ts_operacional.date() if ts_operacional else None,
                reaberta=True,
            )
        return None
    return ultima_entrega


def filtrar_entregas_no_periodo(
    entregas: Iterable[EntregaEfetiva],
    periodo_inicio: date,
    periodo_fim: date,
    motoboy_id: Optional[int] = None,
) -> List[EntregaEfetiva]:
    out: List[EntregaEfetiva] = []
    for e in entregas:
        if e.reaberta:
            continue
        if e.data_confirmacao < periodo_inicio or e.data_confirmacao > periodo_fim:
            continue
        if motoboy_id is not None and e.motoboy_id != motoboy_id:
            continue
        out.append(e)
    return out


def classificar_preview_entrega(
    entrega: Optional[EntregaEfetiva],
    *,
    periodo_inicio: date,
    periodo_fim: date,
    status_atual: Optional[str] = None,
) -> str:
    """Retorna grupo de prévia: incluido | outra_quinzena | ausente | sem_confirmacao | reaberto."""
    status_norm = (status_atual or "").strip().lower()
    if "ausent" in status_norm:
        return "ausente"
    if entrega is None:
        return "sem_confirmacao"
    if entrega.reaberta:
        return "reaberto"
    if entrega.data_confirmacao < periodo_inicio or entrega.data_confirmacao > periodo_fim:
        return "outra_quinzena"
    return "incluido"
