from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar

from sqlalchemy import and_, case, cast, func, or_, select, Date
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from db_utils import run_db_query_with_retry
from models import Saida, SaidaHistorico, User
from saida_operacional_pure import (
    EVENTOS_ATRIBUICAO_VALIDOS,
    EVENTOS_FECHAMENTO_MARCOS,
    EVENTOS_INVALIDANTES,
    EVENTOS_REATRIBUICAO,
    EVENTOS_UI_ULTIMA_ACAO,
    MarcosOperacionaisFechamento,
    ROTULOS_ACAO,
    SaidaOperacionalContext,
    deve_excluir_saida_operacional,
    eh_reatribuicao_real,
    janela_timestamp_periodo,
    marcos_de_ultimos_eventos,
    normalizar_evento_atribuicao,
    resolver_chave_acao,
    resolver_marcos_operacionais_fechamento,
    rotulo_acao_evento,
    timestamp_operacional_marcos,
    timestamp_operacional_saida,
)

EVENTOS_ATRIB_OU_INV = tuple(sorted(EVENTOS_ATRIBUICAO_VALIDOS | EVENTOS_INVALIDANTES))
EVENTOS_MARCOS_TUPLA = tuple(sorted(EVENTOS_FECHAMENTO_MARCOS))

MAX_IDS_POR_LOTE = 1000
T = TypeVar("T")

# Reexport para compatibilidade com imports existentes
__all__ = [
    "EVENTOS_ATRIBUICAO_VALIDOS",
    "EVENTOS_REATRIBUICAO",
    "EVENTOS_INVALIDANTES",
    "EVENTOS_UI_ULTIMA_ACAO",
    "ROTULOS_ACAO",
    "SaidaOperacionalContext",
    "MarcosOperacionaisFechamento",
    "resolver_chave_acao",
    "rotulo_acao_evento",
    "eh_reatribuicao_real",
    "normalizar_evento_atribuicao",
    "resolver_marcos_operacionais_fechamento",
    "carregar_contexto_operacional",
    "carregar_marcos_operacionais_fechamento",
    "deve_excluir_saida_operacional",
    "timestamp_operacional_saida",
    "timestamp_operacional_marcos",
    "filtrar_saidas_por_periodo_operacional",
    "filtrar_saidas_por_periodo_operacional_leve",
    "carregar_saidas_candidatas_periodo",
    "carregar_saidas_fechamento_leve",
    "agregar_saidas_fechamento_resumo",
    "SaidaFechamentoLeve",
    "SaidaFechamentoAgregado",
    "marcos_de_ultimos_eventos",
    "janela_timestamp_periodo",
]


def _chunked(values: Sequence[T], chunk_size: int) -> Iterable[Sequence[T]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser maior que zero")
    for i in range(0, len(values), chunk_size):
        yield values[i : i + chunk_size]


def carregar_contexto_operacional(
    db: Session,
    saida_ids: Iterable[int],
) -> Dict[int, SaidaOperacionalContext]:
    ids = list(dict.fromkeys(int(i) for i in saida_ids if i is not None))
    if not ids:
        return {}

    eventos_filtro = tuple(
        EVENTOS_ATRIBUICAO_VALIDOS | EVENTOS_INVALIDANTES | EVENTOS_UI_ULTIMA_ACAO
    )
    historicos = []
    for ids_lote in _chunked(ids, MAX_IDS_POR_LOTE):
        rows_lote = run_db_query_with_retry(
            db,
            lambda ids_lote=ids_lote: db.execute(
                select(
                    SaidaHistorico.id_saida,
                    SaidaHistorico.evento,
                    SaidaHistorico.user_id,
                    SaidaHistorico.motoboy_id_anterior,
                    SaidaHistorico.timestamp,
                )
                .where(
                    SaidaHistorico.id_saida.in_(ids_lote),
                    SaidaHistorico.evento.in_(eventos_filtro),
                )
                .order_by(
                    SaidaHistorico.id_saida.asc(),
                    SaidaHistorico.timestamp.asc(),
                    SaidaHistorico.id.asc(),
                )
            ).all(),
        )
        historicos.extend(rows_lote)

    estado_por_saida: Dict[int, Dict[str, object]] = {}
    user_ids = set()

    for h in historicos:
        sid = int(h.id_saida)
        evento = ((h.evento or "").strip().lower())
        estado = estado_por_saida.setdefault(
            sid,
            {
                "ultimo": None,
                "op": None,
                "removido_ativo": False,
                "teve_reatribuicao": False,
            },
        )

        estado["ultimo"] = h
        if h.user_id is not None:
            user_ids.add(int(h.user_id))

        if evento in EVENTOS_INVALIDANTES:
            estado["op"] = None
            estado["removido_ativo"] = True
            estado["teve_reatribuicao"] = False
            continue

        motoboy_ant = getattr(h, "motoboy_id_anterior", None)
        if eh_reatribuicao_real(evento, motoboy_ant):
            estado["teve_reatribuicao"] = True
        if evento in EVENTOS_ATRIBUICAO_VALIDOS:
            estado["op"] = h
            estado["removido_ativo"] = False

    from name_normalizer import format_person_full_name

    user_map: Dict[int, str] = {}
    if user_ids:
        rows_user = []
        for user_ids_lote in _chunked(sorted(user_ids), MAX_IDS_POR_LOTE):
            rows_lote = run_db_query_with_retry(
                db,
                lambda user_ids_lote=user_ids_lote: db.execute(
                    select(User.id, User.nome, User.sobrenome, User.username).where(
                        User.id.in_(user_ids_lote)
                    )
                ).all(),
            )
            rows_user.extend(rows_lote)
        user_map = {
            int(uid): format_person_full_name(
                nome, sobrenome, username=uname, fallback=uname or ""
            )
            for uid, nome, sobrenome, uname in rows_user
        }

    out: Dict[int, SaidaOperacionalContext] = {}
    for sid in ids:
        estado = estado_por_saida.get(sid)
        if not estado:
            out[sid] = SaidaOperacionalContext(
                id_saida=sid,
                ultimo_evento=None,
                ultimo_evento_ts=None,
                acao_label=None,
                executado_por="—",
                ultimo_ator_username=None,
                ultimo_ator_user_id=None,
                operacional_evento=None,
                operacional_ts=None,
                leitura_valida=False,
                removido_sem_inicio_ativo=False,
            )
            continue

        ultimo = estado.get("ultimo")
        op = estado.get("op")
        ultimo_evento = (getattr(ultimo, "evento", None) or None) if ultimo is not None else None
        ultimo_user_id = getattr(ultimo, "user_id", None) if ultimo is not None else None
        ultimo_user_id = int(ultimo_user_id) if ultimo_user_id is not None else None
        executado_por = "—"
        if ultimo_evento:
            if ultimo_user_id is not None:
                executado_por = user_map.get(ultimo_user_id) or "—"
            else:
                executado_por = "Sistema"
        op_evento = (getattr(op, "evento", None) or None) if op is not None else None
        op_user_id = getattr(op, "user_id", None) if op is not None else None
        op_user_id = int(op_user_id) if op_user_id is not None else None
        houve_reatribuicao = bool(estado.get("teve_reatribuicao", False))
        ultimo_motoboy_ant = (
            getattr(ultimo, "motoboy_id_anterior", None) if ultimo is not None else None
        )
        ultimo_evento_ui = normalizar_evento_atribuicao(
            ultimo_evento, motoboy_id_anterior=ultimo_motoboy_ant
        )

        out[sid] = SaidaOperacionalContext(
            id_saida=sid,
            ultimo_evento=ultimo_evento_ui or ultimo_evento,
            ultimo_evento_ts=getattr(ultimo, "timestamp", None) if ultimo is not None else None,
            acao_label=rotulo_acao_evento(
                ultimo_evento,
                houve_reatribuicao=houve_reatribuicao,
                motoboy_id_anterior=ultimo_motoboy_ant,
            ),
            executado_por=executado_por,
            ultimo_ator_username=user_map.get(op_user_id) if op_user_id is not None else None,
            ultimo_ator_user_id=op_user_id,
            operacional_evento=op_evento,
            operacional_ts=getattr(op, "timestamp", None) if op is not None else None,
            leitura_valida=op is not None,
            removido_sem_inicio_ativo=bool(estado.get("removido_ativo", False)),
        )

    return out


def carregar_marcos_operacionais_fechamento(
    db: Session,
    saida_ids: Iterable[int],
) -> Dict[int, MarcosOperacionaisFechamento]:
    """Marcos de data operacional: só o último atrib/inv e o último marco por saída.

    Equivalente à máquina de estados completa, sem trazer o histórico inteiro.
    """
    ids = list(dict.fromkeys(int(i) for i in saida_ids if i is not None))
    if not ids:
        return {}

    ult_ai: Dict[int, Tuple[Optional[str], Optional[datetime]]] = {}
    ult_marco: Dict[int, Optional[datetime]] = {}
    for ids_lote in _chunked(ids, MAX_IDS_POR_LOTE):
        rows_ai = run_db_query_with_retry(
            db,
            lambda ids_lote=ids_lote: db.execute(
                select(
                    SaidaHistorico.id_saida,
                    SaidaHistorico.evento,
                    SaidaHistorico.timestamp,
                )
                .where(
                    SaidaHistorico.id_saida.in_(ids_lote),
                    SaidaHistorico.evento.in_(EVENTOS_ATRIB_OU_INV),
                )
                .distinct(SaidaHistorico.id_saida)
                .order_by(
                    SaidaHistorico.id_saida.asc(),
                    SaidaHistorico.timestamp.desc(),
                    SaidaHistorico.id.desc(),
                )
            ).all(),
        )
        for row in rows_ai:
            ult_ai[int(row.id_saida)] = (row.evento, row.timestamp)

        rows_marco = run_db_query_with_retry(
            db,
            lambda ids_lote=ids_lote: db.execute(
                select(
                    SaidaHistorico.id_saida,
                    SaidaHistorico.timestamp,
                )
                .where(
                    SaidaHistorico.id_saida.in_(ids_lote),
                    SaidaHistorico.evento.in_(EVENTOS_MARCOS_TUPLA),
                )
                .distinct(SaidaHistorico.id_saida)
                .order_by(
                    SaidaHistorico.id_saida.asc(),
                    SaidaHistorico.timestamp.desc(),
                    SaidaHistorico.id.desc(),
                )
            ).all(),
        )
        for row in rows_marco:
            ult_marco[int(row.id_saida)] = row.timestamp

    out: Dict[int, MarcosOperacionaisFechamento] = {}
    for sid in ids:
        ev, ts = ult_ai.get(sid, (None, None))
        out[sid] = marcos_de_ultimos_eventos(
            ultimo_atrib_ou_inv_evento=ev,
            ultimo_atrib_ou_inv_ts=ts,
            ultimo_marco_ts=ult_marco.get(sid),
        )
    return out


def filtrar_saidas_por_periodo_operacional(
    db: Session,
    saidas: Sequence[object],
    periodo_inicio: Optional[date],
    periodo_fim: Optional[date],
) -> Tuple[List[object], Dict[int, SaidaOperacionalContext]]:
    if not saidas:
        return [], {}
    ids = [int(getattr(s, "id_saida")) for s in saidas if getattr(s, "id_saida", None) is not None]
    ctx_map = carregar_contexto_operacional(db, ids)
    filtradas: List[object] = []
    for s in saidas:
        sid = getattr(s, "id_saida", None)
        if sid is None:
            continue
        ctx = ctx_map.get(int(sid))
        if deve_excluir_saida_operacional(ctx):
            continue
        ts = timestamp_operacional_saida(ctx, getattr(s, "timestamp", None))
        if ts is None:
            continue
        dia = ts.date()
        if periodo_inicio is not None and dia < periodo_inicio:
            continue
        if periodo_fim is not None and dia > periodo_fim:
            continue
        filtradas.append(s)
    return filtradas, ctx_map


def filtrar_saidas_por_periodo_operacional_leve(
    db: Session,
    saidas: Sequence[object],
    periodo_inicio: Optional[date],
    periodo_fim: Optional[date],
) -> Tuple[List[object], Dict[int, MarcosOperacionaisFechamento]]:
    """Filtra pelo período operacional sem carregar usuários (caminho do fechamento)."""
    if not saidas:
        return [], {}
    ids = [int(getattr(s, "id_saida")) for s in saidas if getattr(s, "id_saida", None) is not None]
    marcos_map = carregar_marcos_operacionais_fechamento(db, ids)
    filtradas: List[object] = []
    for s in saidas:
        sid = getattr(s, "id_saida", None)
        if sid is None:
            continue
        marcos = marcos_map.get(int(sid))
        ts = timestamp_operacional_marcos(marcos, getattr(s, "timestamp", None))
        if ts is None:
            continue
        dia = ts.date()
        if periodo_inicio is not None and dia < periodo_inicio:
            continue
        if periodo_fim is not None and dia > periodo_fim:
            continue
        filtradas.append(s)
    return filtradas, marcos_map


@dataclass
class SaidaFechamentoLeve:
    """Linha enxuta do fechamento (sem ORM completo)."""

    id_saida: int
    timestamp: datetime
    sub_base: Optional[str]
    base: Optional[str]
    status: Optional[str]
    servico: Optional[str]
    is_grande: bool
    categoria_g_id: Optional[int]
    motoboy_id: Optional[int]
    entregador_id: Optional[int]
    entregador: Optional[str]
    codigo: Optional[str]


@dataclass(frozen=True)
class SaidaFechamentoAgregado:
    """Contagem agregada por dia operacional / executor / serviço (sem linha a linha)."""

    data: date
    base: Optional[str]
    motoboy_id: Optional[int]
    entregador_id: Optional[int]
    entregador: Optional[str]
    tipo_servico: str  # shopee | flex | avulso
    is_cancelado: bool
    is_grande: bool
    categoria_g_id: Optional[int]
    qtd: int


def _conds_saidas_fechamento(
    *,
    sub_base: str,
    status_validos: Sequence[str],
    periodo_inicio: Optional[date],
    periodo_fim: Optional[date],
    motoboy_ids: Optional[Sequence[int]],
    entregador_ids: Optional[Sequence[int]],
    exigir_executor: bool,
    base_prefiltro: Optional[str],
) -> Optional[List[ColumnElement]]:
    """None = resultado vazio (executor sem ids)."""
    from fechamento_resumo_utils import base_prefiltro_sql_valores

    conds: List[ColumnElement] = [
        Saida.sub_base == sub_base,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(list(status_validos)),
    ]
    if exigir_executor:
        conds.append(
            or_(
                Saida.entregador_id.isnot(None),
                Saida.entregador.isnot(None),
                Saida.motoboy_id.isnot(None),
            )
        )
    if periodo_inicio is not None and periodo_fim is not None:
        ts_ini, ts_fim_excl = janela_timestamp_periodo(periodo_inicio, periodo_fim)
        conds.extend([Saida.timestamp >= ts_ini, Saida.timestamp < ts_fim_excl])
    elif periodo_inicio is not None:
        ts_ini, _ = janela_timestamp_periodo(periodo_inicio, periodo_inicio)
        conds.append(Saida.timestamp >= ts_ini)
    elif periodo_fim is not None:
        _, ts_fim_excl = janela_timestamp_periodo(periodo_fim, periodo_fim)
        conds.append(Saida.timestamp < ts_fim_excl)

    base_vals = base_prefiltro_sql_valores(base_prefiltro)
    if base_vals:
        conds.append(func.lower(func.trim(Saida.base)).in_(base_vals))

    if motoboy_ids is not None or entregador_ids is not None:
        mids = [int(m) for m in (motoboy_ids or []) if m is not None]
        eids = [int(e) for e in (entregador_ids or []) if e is not None]
        exec_conds = []
        if eids:
            exec_conds.append(Saida.entregador_id.in_(eids))
        if mids:
            exec_conds.append(Saida.motoboy_id.in_(mids))
        if not exec_conds:
            return None
        conds.append(or_(*exec_conds))
    return conds


def _cte_saidas_com_marcos(conds: Sequence[ColumnElement]):
    candidatas = (
        select(
            Saida.id_saida.label("id_saida"),
            Saida.timestamp.label("timestamp"),
            Saida.sub_base.label("sub_base"),
            Saida.base.label("base"),
            Saida.status.label("status"),
            Saida.servico.label("servico"),
            Saida.is_grande.label("is_grande"),
            Saida.categoria_g_id.label("categoria_g_id"),
            Saida.motoboy_id.label("motoboy_id"),
            Saida.entregador_id.label("entregador_id"),
            Saida.entregador.label("entregador"),
            Saida.codigo.label("codigo"),
        )
        .where(*conds)
        .cte("fechamento_candidatas")
    )
    ult_ai = (
        select(
            SaidaHistorico.id_saida.label("id_saida"),
            SaidaHistorico.evento.label("evento"),
            SaidaHistorico.timestamp.label("timestamp"),
        )
        .join(candidatas, candidatas.c.id_saida == SaidaHistorico.id_saida)
        .where(SaidaHistorico.evento.in_(EVENTOS_ATRIB_OU_INV))
        .distinct(SaidaHistorico.id_saida)
        .order_by(
            SaidaHistorico.id_saida.asc(),
            SaidaHistorico.timestamp.desc(),
            SaidaHistorico.id.desc(),
        )
        .cte("fechamento_ult_ai")
    )
    ult_marco = (
        select(
            SaidaHistorico.id_saida.label("id_saida"),
            SaidaHistorico.timestamp.label("timestamp"),
        )
        .join(candidatas, candidatas.c.id_saida == SaidaHistorico.id_saida)
        .where(SaidaHistorico.evento.in_(EVENTOS_MARCOS_TUPLA))
        .distinct(SaidaHistorico.id_saida)
        .order_by(
            SaidaHistorico.id_saida.asc(),
            SaidaHistorico.timestamp.desc(),
            SaidaHistorico.id.desc(),
        )
        .cte("fechamento_ult_marco")
    )
    return candidatas, ult_ai, ult_marco


def _expr_operacional_ts(candidatas, ult_ai, ult_marco):
    invalidantes = tuple(sorted(EVENTOS_INVALIDANTES))
    return case(
        (ult_ai.c.evento.in_(invalidantes), None),
        (ult_ai.c.timestamp.isnot(None), ult_ai.c.timestamp),
        else_=func.coalesce(ult_marco.c.timestamp, candidatas.c.timestamp),
    )


def _expr_tipo_servico(servico_col):
    """Espelha `_normalizar_servico` / `normalizar_servico` (shopee → ml/mercado → avulso)."""
    s = func.lower(func.coalesce(servico_col, ""))
    return case(
        (s.like("%shopee%"), "shopee"),
        (or_(s.like("%ml%"), s.like("%mercado%")), "flex"),
        else_="avulso",
    )


def carregar_saidas_fechamento_leve(
    db: Session,
    *,
    sub_base: str,
    status_validos: Sequence[str],
    periodo_inicio: Optional[date],
    periodo_fim: Optional[date],
    motoboy_ids: Optional[Sequence[int]] = None,
    entregador_ids: Optional[Sequence[int]] = None,
    exigir_executor: bool = True,
    base_prefiltro: Optional[str] = None,
) -> Tuple[List[SaidaFechamentoLeve], Dict[int, MarcosOperacionaisFechamento]]:
    """Candidatas do fechamento com marcos via DISTINCT ON (sem varrer o histórico).

    A janela de candidatos continua sendo `sub_base` + `timestamp` (igual ao resumo atual).
    O join no histórico traz só o último atrib/inv e o último marco por saída.
    `base_prefiltro` reduz candidatos no SQL; a filtragem final de acento fica no chamador.
    """
    conds = _conds_saidas_fechamento(
        sub_base=sub_base,
        status_validos=status_validos,
        periodo_inicio=periodo_inicio,
        periodo_fim=periodo_fim,
        motoboy_ids=motoboy_ids,
        entregador_ids=entregador_ids,
        exigir_executor=exigir_executor,
        base_prefiltro=base_prefiltro,
    )
    if conds is None:
        return [], {}

    candidatas, ult_ai, ult_marco = _cte_saidas_com_marcos(conds)
    stmt = (
        select(
            candidatas.c.id_saida,
            candidatas.c.timestamp,
            candidatas.c.sub_base,
            candidatas.c.base,
            candidatas.c.status,
            candidatas.c.servico,
            candidatas.c.is_grande,
            candidatas.c.categoria_g_id,
            candidatas.c.motoboy_id,
            candidatas.c.entregador_id,
            candidatas.c.entregador,
            candidatas.c.codigo,
            ult_ai.c.evento.label("ult_ai_evento"),
            ult_ai.c.timestamp.label("ult_ai_ts"),
            ult_marco.c.timestamp.label("ult_marco_ts"),
        )
        .select_from(candidatas)
        .outerjoin(ult_ai, ult_ai.c.id_saida == candidatas.c.id_saida)
        .outerjoin(ult_marco, ult_marco.c.id_saida == candidatas.c.id_saida)
    )

    rows_db = run_db_query_with_retry(db, lambda: db.execute(stmt).all())
    filtradas: List[SaidaFechamentoLeve] = []
    marcos_map: Dict[int, MarcosOperacionaisFechamento] = {}
    for row in rows_db:
        sid = int(row.id_saida)
        marcos = marcos_de_ultimos_eventos(
            ultimo_atrib_ou_inv_evento=row.ult_ai_evento,
            ultimo_atrib_ou_inv_ts=row.ult_ai_ts,
            ultimo_marco_ts=row.ult_marco_ts,
        )
        marcos_map[sid] = marcos
        ts = timestamp_operacional_marcos(marcos, row.timestamp)
        if ts is None:
            continue
        dia = ts.date()
        if periodo_inicio is not None and dia < periodo_inicio:
            continue
        if periodo_fim is not None and dia > periodo_fim:
            continue
        filtradas.append(
            SaidaFechamentoLeve(
                id_saida=sid,
                timestamp=row.timestamp,
                sub_base=row.sub_base,
                base=row.base,
                status=row.status,
                servico=row.servico,
                is_grande=bool(row.is_grande),
                categoria_g_id=row.categoria_g_id,
                motoboy_id=row.motoboy_id,
                entregador_id=row.entregador_id,
                entregador=row.entregador,
                codigo=row.codigo,
            )
        )
    return filtradas, marcos_map


def agregar_saidas_fechamento_resumo(
    db: Session,
    *,
    sub_base: str,
    status_validos: Sequence[str],
    periodo_inicio: date,
    periodo_fim: date,
    motoboy_ids: Optional[Sequence[int]] = None,
    entregador_ids: Optional[Sequence[int]] = None,
    exigir_executor: bool = True,
    base_prefiltro: Optional[str] = None,
) -> List[SaidaFechamentoAgregado]:
    """Agrega no SQL por dia operacional / executor / serviço — sem trazer pacote a pacote."""
    conds = _conds_saidas_fechamento(
        sub_base=sub_base,
        status_validos=status_validos,
        periodo_inicio=periodo_inicio,
        periodo_fim=periodo_fim,
        motoboy_ids=motoboy_ids,
        entregador_ids=entregador_ids,
        exigir_executor=exigir_executor,
        base_prefiltro=base_prefiltro,
    )
    if conds is None:
        return []

    candidatas, ult_ai, ult_marco = _cte_saidas_com_marcos(conds)
    op_ts = _expr_operacional_ts(candidatas, ult_ai, ult_marco)
    is_grande = func.coalesce(candidatas.c.is_grande, False)
    com_data = (
        select(
            candidatas.c.base.label("base"),
            candidatas.c.motoboy_id.label("motoboy_id"),
            candidatas.c.entregador_id.label("entregador_id"),
            candidatas.c.entregador.label("entregador"),
            candidatas.c.status.label("status"),
            candidatas.c.servico.label("servico"),
            is_grande.label("is_grande"),
            candidatas.c.categoria_g_id.label("categoria_g_id"),
            op_ts.label("operacional_ts"),
        )
        .select_from(candidatas)
        .outerjoin(ult_ai, ult_ai.c.id_saida == candidatas.c.id_saida)
        .outerjoin(ult_marco, ult_marco.c.id_saida == candidatas.c.id_saida)
        .where(op_ts.isnot(None))
        .cte("fechamento_com_data")
    )
    dia = cast(com_data.c.operacional_ts, Date)
    tipo = _expr_tipo_servico(com_data.c.servico)
    is_cancel = func.lower(func.coalesce(com_data.c.status, "")).like("%cancel%")
    stmt = (
        select(
            dia.label("data"),
            com_data.c.base,
            com_data.c.motoboy_id,
            com_data.c.entregador_id,
            com_data.c.entregador,
            tipo.label("tipo_servico"),
            is_cancel.label("is_cancelado"),
            com_data.c.is_grande,
            com_data.c.categoria_g_id,
            func.count().label("qtd"),
        )
        .where(dia >= periodo_inicio, dia <= periodo_fim)
        .group_by(
            dia,
            com_data.c.base,
            com_data.c.motoboy_id,
            com_data.c.entregador_id,
            com_data.c.entregador,
            tipo,
            is_cancel,
            com_data.c.is_grande,
            com_data.c.categoria_g_id,
        )
    )
    rows = run_db_query_with_retry(db, lambda: db.execute(stmt).all())
    out: List[SaidaFechamentoAgregado] = []
    for row in rows:
        out.append(
            SaidaFechamentoAgregado(
                data=row.data,
                base=row.base,
                motoboy_id=int(row.motoboy_id) if row.motoboy_id is not None else None,
                entregador_id=int(row.entregador_id) if row.entregador_id is not None else None,
                entregador=row.entregador,
                tipo_servico=str(row.tipo_servico or "avulso"),
                is_cancelado=bool(row.is_cancelado),
                is_grande=bool(row.is_grande),
                categoria_g_id=int(row.categoria_g_id) if row.categoria_g_id is not None else None,
                qtd=int(row.qtd or 0),
            )
        )
    return out


def carregar_saidas_candidatas_periodo(
    db: Session,
    *,
    sub_base: str,
    motoboy_ids: Sequence[int],
    inicio: date,
    fim: date,
) -> List[Saida]:
    """Candidatas do período sem varrer o histórico inteiro do motoboy.

    Inclui:
    - Saida.data no intervalo;
    - Saida.timestamp no intervalo;
    - pacote antigo reatribuído no intervalo (evento de atribuição no histórico).
    """
    ids = list(dict.fromkeys(int(m) for m in motoboy_ids if m is not None))
    if not ids or not (sub_base or "").strip():
        return []
    ts_ini, ts_fim_excl = janela_timestamp_periodo(inicio, fim)
    hist_ids = (
        select(SaidaHistorico.id_saida)
        .join(Saida, Saida.id_saida == SaidaHistorico.id_saida)
        .where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id.in_(ids),
            SaidaHistorico.evento.in_(tuple(EVENTOS_ATRIBUICAO_VALIDOS)),
            SaidaHistorico.timestamp >= ts_ini,
            SaidaHistorico.timestamp < ts_fim_excl,
        )
    )
    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id.in_(ids),
        Saida.codigo.isnot(None),
        or_(
            and_(Saida.data >= inicio, Saida.data <= fim),
            and_(Saida.timestamp >= ts_ini, Saida.timestamp < ts_fim_excl),
            Saida.id_saida.in_(hist_ids),
        ),
    )
    return list(db.scalars(stmt).all())
