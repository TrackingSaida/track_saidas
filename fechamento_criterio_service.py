"""Serviço de critério e cálculo centralizado do fechamento de motoboy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from fechamento_criterio_pure import (
    EVENTOS_ENTREGA,
    MODO_CONFIRMACAO_ENTREGA,
    MODO_OPERACIONAL,
    EntregaEfetiva,
    HistoricoEventoLite,
    classificar_preview_entrega,
    filtrar_entregas_no_periodo,
    normalizar_modo,
    resolver_entrega_efetiva,
)
from models import (
    EntregadorFechamento,
    EntregadorFechamentoItem,
    Saida,
    SaidaHistorico,
    SubBaseFechamentoConfig,
)
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional


STATUS_OPERACIONAIS = [
    "saiu",
    "saiu pra entrega",
    "saiu_pra_entrega",
    "saiu_para_entrega",
    "em_rota",
    "entregue",
    "ausente",
    "cancelado",
    "cancelados",
]


@dataclass
class FechamentoItemCalc:
    id_saida: int
    codigo: Optional[str]
    id_motoboy: Optional[int]
    id_entregador: Optional[int]
    servico: Optional[str]
    status_evento: str
    valor: Decimal
    is_grande: bool
    data_operacional: Optional[date]
    data_confirmacao: Optional[date]
    id_historico_atribuicao: Optional[int]
    id_historico_confirmacao: Optional[int]
    dia_ref: date


def get_modo_fechamento(db: Session, sub_base: str) -> str:
    row = db.scalars(
        select(SubBaseFechamentoConfig).where(SubBaseFechamentoConfig.sub_base == sub_base)
    ).first()
    if not row:
        return MODO_OPERACIONAL
    return normalizar_modo(row.modo)


def upsert_modo_fechamento(
    db: Session,
    sub_base: str,
    modo: str,
    updated_by: Optional[int] = None,
) -> SubBaseFechamentoConfig:
    modo_norm = normalizar_modo(modo)
    row = db.scalars(
        select(SubBaseFechamentoConfig).where(SubBaseFechamentoConfig.sub_base == sub_base)
    ).first()
    if row is None:
        row = SubBaseFechamentoConfig(
            sub_base=sub_base,
            modo=modo_norm,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.modo = modo_norm
        row.updated_by = updated_by
        row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def _normalizar_servico(servico: Optional[str]) -> str:
    s = (servico or "").lower()
    if "shopee" in s:
        return "shopee"
    if "ml" in s or "mercado" in s:
        return "flex"
    return "avulso"


def _preco_por_tipo(precos: Dict[str, Decimal], tipo: str) -> Decimal:
    if tipo == "shopee":
        return Decimal(str(precos.get("shopee_valor") or 0))
    if tipo == "flex":
        return Decimal(str(precos.get("ml_valor") or 0))
    return Decimal(str(precos.get("avulso_valor") or 0))


def _carregar_historico_por_saidas(
    db: Session,
    saida_ids: Sequence[int],
) -> Dict[int, List[HistoricoEventoLite]]:
    ids = sorted({int(i) for i in saida_ids if i is not None})
    if not ids:
        return {}
    rows = db.scalars(
        select(SaidaHistorico)
        .where(SaidaHistorico.id_saida.in_(ids))
        .order_by(SaidaHistorico.id_saida, SaidaHistorico.timestamp, SaidaHistorico.id)
    ).all()
    out: Dict[int, List[HistoricoEventoLite]] = {}
    for h in rows:
        sid = int(h.id_saida)
        out.setdefault(sid, []).append(
            HistoricoEventoLite(
                id=int(h.id) if h.id is not None else None,
                evento=h.evento or "",
                timestamp=h.timestamp,
                motoboy_id_anterior=h.motoboy_id_anterior,
                motoboy_id_novo=h.motoboy_id_novo,
            )
        )
    return out


def _saidas_candidatas_confirmacao(
    db: Session,
    sub_base: str,
    periodo_inicio: date,
    periodo_fim: date,
    motoboy_id: Optional[int] = None,
) -> List[Saida]:
    """Busca saídas com evento de entrega no período (ou atribuição atual do motoboy)."""
    hist_ids = set(
        db.scalars(
            select(SaidaHistorico.id_saida).where(
                SaidaHistorico.evento.in_(tuple(EVENTOS_ENTREGA)),
                func.date(SaidaHistorico.timestamp) >= periodo_inicio,
                func.date(SaidaHistorico.timestamp) <= periodo_fim,
            )
        ).all()
    )
    if motoboy_id is not None:
        # Inclui também saídas do motoboy ainda sem baixa / reabertas para prévia.
        atuais = set(
            db.scalars(
                select(Saida.id_saida).where(
                    Saida.sub_base == sub_base,
                    Saida.motoboy_id == motoboy_id,
                    Saida.codigo.isnot(None),
                )
            ).all()
        )
        hist_ids |= atuais
    if not hist_ids:
        return []
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.codigo.isnot(None),
        Saida.id_saida.in_(sorted(hist_ids)),
    )
    if motoboy_id is not None:
        # Não filtra só pelo motoboy atual: entrega pode ter sido do motoboy A
        # e a saída estar com outro. O filtro de motoboy é na elegibilidade.
        pass
    return list(db.scalars(stmt).all())


def listar_itens_confirmacao_entrega(
    db: Session,
    *,
    sub_base: str,
    periodo_inicio: date,
    periodo_fim: date,
    motoboy_id: Optional[int],
    precos: Dict[str, Decimal],
    toggle_pacote_g: bool = False,
) -> Tuple[List[FechamentoItemCalc], Dict[str, List[Dict[str, Any]]]]:
    saidas = _saidas_candidatas_confirmacao(
        db, sub_base, periodo_inicio, periodo_fim, motoboy_id=motoboy_id
    )
    hist_map = _carregar_historico_por_saidas(db, [int(s.id_saida) for s in saidas])
    itens: List[FechamentoItemCalc] = []
    preview: Dict[str, List[Dict[str, Any]]] = {
        "incluido": [],
        "outra_quinzena": [],
        "ausente": [],
        "sem_confirmacao": [],
        "reaberto": [],
    }

    for saida in saidas:
        sid = int(saida.id_saida)
        entrega = resolver_entrega_efetiva(
            sid,
            hist_map.get(sid, []),
            motoboy_atual=getattr(saida, "motoboy_id", None),
        )
        grupo = classificar_preview_entrega(
            entrega,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            status_atual=saida.status,
        )
        info = {
            "id_saida": sid,
            "codigo": saida.codigo,
            "servico": saida.servico,
            "status": saida.status,
            "motoboy_id": entrega.motoboy_id if entrega else getattr(saida, "motoboy_id", None),
            "data_operacional": entrega.data_operacional.isoformat() if entrega and entrega.data_operacional else None,
            "data_confirmacao": (
                entrega.data_confirmacao.isoformat()
                if entrega and not entrega.reaberta and entrega.data_confirmacao != date.min
                else None
            ),
        }
        # Prévia: para motoboy específico, só mostra saídas relacionadas a ele.
        motoboy_ref = info["motoboy_id"]
        if motoboy_id is not None and motoboy_ref is not None and int(motoboy_ref) != int(motoboy_id):
            if grupo != "incluido":
                continue
        preview[grupo].append(info)

        if grupo != "incluido" or entrega is None:
            continue
        if motoboy_id is not None and entrega.motoboy_id != motoboy_id:
            continue

        tipo = _normalizar_servico(saida.servico)
        delta = _preco_por_tipo(precos, tipo)
        if toggle_pacote_g and bool(getattr(saida, "is_grande", False)):
            delta = (delta * Decimal("2")).quantize(Decimal("0.01"))
        itens.append(
            FechamentoItemCalc(
                id_saida=sid,
                codigo=saida.codigo,
                id_motoboy=entrega.motoboy_id,
                id_entregador=getattr(saida, "entregador_id", None),
                servico=saida.servico,
                status_evento="entregue",
                valor=delta.quantize(Decimal("0.01")),
                is_grande=bool(getattr(saida, "is_grande", False)),
                data_operacional=entrega.data_operacional,
                data_confirmacao=entrega.data_confirmacao,
                id_historico_atribuicao=entrega.id_historico_atribuicao,
                id_historico_confirmacao=entrega.id_historico_confirmacao,
                dia_ref=entrega.data_confirmacao,
            )
        )
    return itens, preview


def listar_itens_operacional(
    db: Session,
    *,
    sub_base: str,
    periodo_inicio: date,
    periodo_fim: date,
    motoboy_id: Optional[int] = None,
    entregador_id: Optional[int] = None,
    entregador_ids: Optional[Iterable[int]] = None,
    motoboy_ids: Optional[Iterable[int]] = None,
    precos: Dict[str, Decimal],
    toggle_pacote_g: bool = False,
) -> List[FechamentoItemCalc]:
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(STATUS_OPERACIONAIS),
    )
    stmt = stmt.where(Saida.timestamp >= datetime.combine(periodo_inicio, datetime.min.time()))
    stmt = stmt.where(
        Saida.timestamp < datetime.combine(periodo_fim + timedelta(days=1), datetime.min.time())
    )
    conds = []
    if motoboy_id is not None:
        conds.append(Saida.motoboy_id == motoboy_id)
    if entregador_id is not None:
        conds.append(Saida.entregador_id == entregador_id)
    if entregador_ids:
        conds.append(Saida.entregador_id.in_(sorted({int(x) for x in entregador_ids})))
    if motoboy_ids:
        conds.append(Saida.motoboy_id.in_(sorted({int(x) for x in motoboy_ids})))
    if conds:
        stmt = stmt.where(or_(*conds))
    rows_raw = list(db.scalars(stmt).all())
    rows, op_ctx_map = filtrar_saidas_por_periodo_operacional(
        db, rows_raw, periodo_inicio, periodo_fim
    )
    itens: List[FechamentoItemCalc] = []
    for saida in rows:
        status_norm = (saida.status or "").strip().lower()
        is_cancelado = "cancel" in status_norm
        tipo = _normalizar_servico(saida.servico)
        delta = _preco_por_tipo(precos, tipo)
        valor = (-delta if is_cancelado else delta).quantize(Decimal("0.01"))
        if toggle_pacote_g and bool(getattr(saida, "is_grande", False)):
            valor = (valor + (-delta if is_cancelado else delta)).quantize(Decimal("0.01"))
        ctx = op_ctx_map.get(saida.id_saida)
        op_ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or saida.timestamp
        dia = op_ts.date() if op_ts else periodo_inicio
        itens.append(
            FechamentoItemCalc(
                id_saida=int(saida.id_saida),
                codigo=saida.codigo,
                id_motoboy=getattr(saida, "motoboy_id", None),
                id_entregador=getattr(saida, "entregador_id", None),
                servico=saida.servico,
                status_evento=status_norm or "saiu",
                valor=valor,
                is_grande=bool(getattr(saida, "is_grande", False)),
                data_operacional=dia,
                data_confirmacao=None,
                id_historico_atribuicao=None,
                id_historico_confirmacao=None,
                dia_ref=dia,
            )
        )
    return itens


def calcular_itens_fechamento(
    db: Session,
    *,
    sub_base: str,
    periodo_inicio: date,
    periodo_fim: date,
    precos: Dict[str, Decimal],
    modo: Optional[str] = None,
    motoboy_id: Optional[int] = None,
    entregador_id: Optional[int] = None,
    entregador_ids: Optional[Iterable[int]] = None,
    motoboy_ids: Optional[Iterable[int]] = None,
    toggle_pacote_g: bool = False,
    com_preview: bool = False,
) -> Tuple[List[FechamentoItemCalc], Decimal, Optional[Dict[str, List[Dict[str, Any]]]]]:
    modo_norm = normalizar_modo(modo or get_modo_fechamento(db, sub_base))
    preview = None
    if modo_norm == MODO_CONFIRMACAO_ENTREGA:
        itens, preview_map = listar_itens_confirmacao_entrega(
            db,
            sub_base=sub_base,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            motoboy_id=motoboy_id,
            precos=precos,
            toggle_pacote_g=toggle_pacote_g,
        )
        if com_preview:
            preview = preview_map
    else:
        itens = listar_itens_operacional(
            db,
            sub_base=sub_base,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            motoboy_id=motoboy_id,
            entregador_id=entregador_id,
            entregador_ids=entregador_ids,
            motoboy_ids=motoboy_ids,
            precos=precos,
            toggle_pacote_g=toggle_pacote_g,
        )
    total = sum((i.valor for i in itens), Decimal("0.00")).quantize(Decimal("0.01"))
    return itens, total, preview


def saidas_ja_fechadas(
    db: Session,
    saida_ids: Sequence[int],
    *,
    excluir_fechamento_id: Optional[int] = None,
) -> List[EntregadorFechamentoItem]:
    ids = sorted({int(i) for i in saida_ids if i is not None})
    if not ids:
        return []
    stmt = select(EntregadorFechamentoItem).where(EntregadorFechamentoItem.id_saida.in_(ids))
    if excluir_fechamento_id is not None:
        stmt = stmt.where(EntregadorFechamentoItem.id_fechamento != excluir_fechamento_id)
    return list(db.scalars(stmt).all())


def persistir_itens_fechamento(
    db: Session,
    id_fechamento: int,
    itens: Sequence[FechamentoItemCalc],
) -> None:
    for item in itens:
        db.add(
            EntregadorFechamentoItem(
                id_fechamento=id_fechamento,
                id_saida=item.id_saida,
                codigo=item.codigo,
                id_motoboy=item.id_motoboy,
                id_entregador=item.id_entregador,
                servico=item.servico,
                status_evento=item.status_evento,
                valor=item.valor,
                is_grande=item.is_grande,
                data_operacional=item.data_operacional,
                data_confirmacao=item.data_confirmacao,
                id_historico_atribuicao=item.id_historico_atribuicao,
                id_historico_confirmacao=item.id_historico_confirmacao,
            )
        )


def buscar_fechamento_cobrindo_data(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
) -> Optional[EntregadorFechamento]:
    return db.scalars(
        select(EntregadorFechamento).where(
            EntregadorFechamento.sub_base == sub_base,
            EntregadorFechamento.id_motoboy == motoboy_id,
            EntregadorFechamento.periodo_inicio <= data_ref,
            EntregadorFechamento.periodo_fim >= data_ref,
            func.upper(EntregadorFechamento.status).in_(("GERADO", "REAJUSTADO", "FECHADO")),
        )
    ).first()


def entrega_efetiva_da_saida(db: Session, saida: Saida) -> Optional[EntregaEfetiva]:
    hist = _carregar_historico_por_saidas(db, [int(saida.id_saida)])
    return resolver_entrega_efetiva(
        int(saida.id_saida),
        hist.get(int(saida.id_saida), []),
        motoboy_atual=getattr(saida, "motoboy_id", None),
    )


def impacto_reatribuicao_entregue(
    db: Session,
    *,
    sub_base: str,
    saida: Saida,
    motoboy_anterior: Optional[int],
) -> Optional[Dict[str, Any]]:
    """
    Se a entrega efetiva do motoboy anterior já está coberta por fechamento gerado
    de outra quinzena (periodo_fim < hoje), retorna o impacto para exigir confirmação.
    Dentro da mesma quinzena ainda aberta, retorna None (reatribuição livre).
    """
    if motoboy_anterior is None:
        return None
    entrega = entrega_efetiva_da_saida(db, saida)
    if entrega is None or entrega.reaberta or entrega.motoboy_id is None:
        return None
    fech = buscar_fechamento_cobrindo_data(
        db,
        sub_base=sub_base,
        motoboy_id=int(entrega.motoboy_id),
        data_ref=entrega.data_confirmacao,
    )
    if fech is None:
        return None
    # Quinzena ainda "em aberto" para operação do dia: libera reatribuição sem trava.
    if fech.periodo_fim >= date.today():
        return None
    return {
        "code": "FECHAMENTO_IMPACTADO",
        "message": (
            "Este pedido já entrou no fechamento de outra quinzena. "
            "A reatribuição exige confirmação administrativa e não altera "
            "automaticamente o fechamento anterior."
        ),
        "id_fechamento": fech.id_fechamento,
        "periodo_inicio": fech.periodo_inicio.isoformat(),
        "periodo_fim": fech.periodo_fim.isoformat(),
        "motoboy_id": int(entrega.motoboy_id),
        "data_confirmacao": entrega.data_confirmacao.isoformat(),
        "codigo": getattr(saida, "codigo", None),
    }


# Reexport útil para testes
__all__ = [
    "MODO_OPERACIONAL",
    "MODO_CONFIRMACAO_ENTREGA",
    "FechamentoItemCalc",
    "get_modo_fechamento",
    "upsert_modo_fechamento",
    "calcular_itens_fechamento",
    "saidas_ja_fechadas",
    "persistir_itens_fechamento",
    "buscar_fechamento_cobrindo_data",
    "entrega_efetiva_da_saida",
    "filtrar_entregas_no_periodo",
]
