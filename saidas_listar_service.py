"""Helper interno da listagem de Registros (GET /saidas/listar).

Objetivos:
- paginar com LIMIT/OFFSET após filtro operacional;
- agregar totalizadores sem hidratar ORM completo do período;
- enriquecer histórico/nomes apenas da página;
- preservar semântica operacional e isolamento por sub_base.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from saida_operacional_pure import (
    EVENTOS_ATRIBUICAO_VALIDOS,
    EVENTOS_INVALIDANTES,
    EVENTOS_REATRIBUICAO,
    EVENTOS_UI_ULTIMA_ACAO,
    SaidaOperacionalContext,
    deve_excluir_saida_operacional,
    rotulo_acao_evento,
    timestamp_operacional_saida,
)

# SQLAlchemy / models são importados sob demanda nas rotinas de banco
# para permitir testes unitários da lógica pura sem dependências instaladas.

MAX_LISTAR_LIMIT = 500
MAX_IDS_POR_LOTE = 250
MAX_IDS_MOTOBOY = 5000


@dataclass
class SaidaListRow:
    id_saida: int
    timestamp: datetime
    sub_base: Optional[str]
    username: Optional[str]
    entregador: Optional[str]
    entregador_id: Optional[int]
    motoboy_id: Optional[int]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    base: Optional[str]
    is_grande: bool


def clamp_listar_limit(limit: Optional[int]) -> Optional[int]:
    if limit is None:
        return None
    value = int(limit)
    if value < 0:
        return 0
    return min(value, MAX_LISTAR_LIMIT)


def _norm_text(value: Optional[str]) -> str:
    import unicodedata

    return unicodedata.normalize("NFD", (value or "").strip().lower()).encode("ascii", "ignore").decode("ascii")


def _parse_multi_values(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for raw in values or []:
        if raw is None:
            continue
        for part in str(raw).split(","):
            token = part.strip()
            if token:
                out.append(token)
    return out


def _status_group_aliases(token: str) -> List[str]:
    key = _norm_text(token).replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    groups = {
        "saiu": ["saiu", "saiu para entrega", "saiu pra entrega", "saiu_pra_entrega", "saiu_para_entrega"],
        "saiu para entrega": ["saiu", "saiu para entrega", "saiu pra entrega", "saiu_pra_entrega", "saiu_para_entrega"],
        "em rota": ["em rota", "em_rota"],
        "entregue": ["entregue"],
        "ausente": ["ausente"],
        "coletado": ["coletado"],
        "nao coletado": ["nao coletado", "não coletado"],
        "cancelado": ["cancelado", "cancelados"],
    }
    normalized = groups.get(key, [key])
    return sorted({v for v in normalized if v})


def _servico_text_expr(expr):
    from sqlalchemy import func

    return func.coalesce(func.unaccent(func.lower(expr)), "")


def _servico_is_shopee_expr(expr):
    return _servico_text_expr(expr).like("%shopee%")


def _servico_is_mercado_expr(expr):
    srv = _servico_text_expr(expr)
    return srv.like("%mercado%") | srv.like("%flex%") | srv.like("%ml%")


def _chunked(values: Sequence[int], chunk_size: int) -> Iterable[Sequence[int]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser maior que zero")
    for i in range(0, len(values), chunk_size):
        yield values[i : i + chunk_size]


def _acao_equivalente(evento_norm: str) -> str:
    from saida_operacional_pure import resolver_chave_acao

    return resolver_chave_acao(evento_norm) or ""


def build_operacional_ctx_from_historico_rows(
    saida_ids: Sequence[int],
    historicos: Sequence[Any],
    user_map: Optional[Dict[int, str]] = None,
) -> Dict[int, SaidaOperacionalContext]:
    """Constrói contexto operacional a partir de tuples/ORM de histórico (ordem ASC)."""
    ids = list(dict.fromkeys(int(i) for i in saida_ids if i is not None))
    if not ids:
        return {}

    user_map = user_map or {}
    estado_por_saida: Dict[int, Dict[str, object]] = {}

    for h in historicos:
        sid = int(getattr(h, "id_saida"))
        evento = ((getattr(h, "evento", None) or "").strip().lower())
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
        if evento in EVENTOS_INVALIDANTES:
            estado["op"] = None
            estado["removido_ativo"] = True
            estado["teve_reatribuicao"] = False
            continue
        if evento in EVENTOS_REATRIBUICAO:
            estado["teve_reatribuicao"] = True
        if evento in EVENTOS_ATRIBUICAO_VALIDOS:
            estado["op"] = h
            estado["removido_ativo"] = False

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

        out[sid] = SaidaOperacionalContext(
            id_saida=sid,
            ultimo_evento=ultimo_evento,
            ultimo_evento_ts=getattr(ultimo, "timestamp", None) if ultimo is not None else None,
            acao_label=rotulo_acao_evento(ultimo_evento, houve_reatribuicao=houve_reatribuicao),
            executado_por=executado_por,
            ultimo_ator_username=user_map.get(op_user_id) if op_user_id is not None else None,
            ultimo_ator_user_id=op_user_id,
            operacional_evento=op_evento,
            operacional_ts=getattr(op, "timestamp", None) if op is not None else None,
            leitura_valida=op is not None,
            removido_sem_inicio_ativo=bool(estado.get("removido_ativo", False)),
        )
    return out


def filtrar_ordenar_agregar_listagem(
    rows: Sequence[SaidaListRow],
    ctx_map: Dict[int, SaidaOperacionalContext],
    *,
    de: Optional[date],
    ate: Optional[date],
    entregador_filter_norm: str = "",
    executor_nome_map: Optional[Dict[int, Optional[str]]] = None,
    acao_tokens: Optional[List[str]] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Tuple[List[SaidaListRow], Dict[str, int], Dict[int, SaidaOperacionalContext]]:
    """Aplica filtro operacional, entregador/ação, ordenação determinística, totais e slice."""
    executor_nome_map = executor_nome_map or {}
    filtradas: List[SaidaListRow] = []

    for row in rows:
        ctx = ctx_map.get(int(row.id_saida))
        if deve_excluir_saida_operacional(ctx):
            continue
        ts = timestamp_operacional_saida(ctx, row.timestamp)
        if ts is None:
            continue
        dia = ts.date()
        if de is not None and dia < de:
            continue
        if ate is not None and dia > ate:
            continue
        filtradas.append(row)

    if entregador_filter_norm:
        filtradas = [
            r
            for r in filtradas
            if _norm_text(executor_nome_map.get(int(r.id_saida)) or r.entregador) == entregador_filter_norm
        ]

    if acao_tokens:
        allowed_eventos = set()
        allowed_labels = set()
        for token in acao_tokens:
            token_norm = " ".join(token.split())
            token_key = token_norm.replace(" ", "_")
            allowed_eventos.add(token_key)
            allowed_labels.add(token_norm)
            label_canonico = rotulo_acao_evento(token_key)
            if label_canonico:
                allowed_labels.add(_norm_text(label_canonico))
            if token_key in {"reatribuido", "reatribuido_em_rota"}:
                allowed_eventos.add("reatribuido")
                allowed_eventos.add("reatribuido_em_rota")
                allowed_labels.add(_norm_text(rotulo_acao_evento("reatribuido") or ""))
                allowed_labels.add(_norm_text(rotulo_acao_evento("reatribuido_em_rota") or ""))

        next_rows: List[SaidaListRow] = []
        for r in filtradas:
            ctx = ctx_map.get(r.id_saida)
            if ctx is None:
                continue
            if _norm_text(_acao_equivalente(ctx.ultimo_evento or "")).replace("_", " ") in allowed_labels:
                next_rows.append(r)
                continue
            if _norm_text(ctx.acao_label or "") in allowed_labels:
                next_rows.append(r)
                continue
            if _acao_equivalente(ctx.ultimo_evento or "") in allowed_eventos:
                next_rows.append(r)
        filtradas = next_rows

    def _sort_key(r: SaidaListRow):
        ctx = ctx_map.get(r.id_saida)
        op_ts = None
        if ctx and ctx.operacional_ts:
            op_ts = ctx.operacional_ts
        elif ctx and ctx.ultimo_evento_ts:
            op_ts = ctx.ultimo_evento_ts
        else:
            op_ts = r.timestamp
        return (op_ts or r.timestamp, int(r.id_saida))

    filtradas.sort(key=_sort_key, reverse=True)

    sum_shopee = 0
    sum_mercado = 0
    sum_avulso = 0
    for r in filtradas:
        srv = (r.servico or "").strip().lower()
        if ("shopee" in srv) or ("spx" in srv):
            sum_shopee += 1
        elif (
            ("mercado livre" in srv)
            or ("mercado_livre" in srv)
            or ("mercadolivre" in srv)
            or (" ml" in f" {srv}")
            or ("flex" in srv)
        ):
            sum_mercado += 1
        else:
            sum_avulso += 1

    totals = {
        "total": len(filtradas),
        "sumShopee": sum_shopee,
        "sumMercado": sum_mercado,
        "sumAvulso": sum_avulso,
    }

    start_idx = max(0, int(offset or 0))
    if limit is not None:
        end_idx = start_idx + max(0, int(limit))
        page = filtradas[start_idx:end_idx]
    else:
        page = filtradas[start_idx:]

    return page, totals, ctx_map


def _load_historico_tuples(db, ids: Sequence[int]) -> List[Any]:
    from sqlalchemy import select

    from db_utils import run_db_query_with_retry
    from models import SaidaHistorico

    if not ids:
        return []
    eventos_filtro = tuple(EVENTOS_ATRIBUICAO_VALIDOS | EVENTOS_INVALIDANTES | EVENTOS_UI_ULTIMA_ACAO)
    historicos: List[Any] = []
    for ids_lote in _chunked(list(ids), MAX_IDS_POR_LOTE):
        rows_lote = run_db_query_with_retry(
            db,
            lambda ids_lote=ids_lote: db.execute(
                select(
                    SaidaHistorico.id,
                    SaidaHistorico.id_saida,
                    SaidaHistorico.evento,
                    SaidaHistorico.timestamp,
                    SaidaHistorico.user_id,
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
    return historicos


def _load_user_map(db, user_ids: Sequence[int]) -> Dict[int, str]:
    from sqlalchemy import select

    from db_utils import run_db_query_with_retry
    from models import User

    if not user_ids:
        return {}
    rows_user = []
    for user_ids_lote in _chunked(sorted(set(int(u) for u in user_ids)), MAX_IDS_POR_LOTE):
        rows_lote = run_db_query_with_retry(
            db,
            lambda user_ids_lote=user_ids_lote: db.execute(
                select(User.id, User.username).where(User.id.in_(user_ids_lote))
            ).all(),
        )
        rows_user.extend(rows_lote)
    return {int(uid): (uname or "") for uid, uname in rows_user}


def _load_motoboy_nome_map(db, motoboy_ids: Sequence[int]) -> Dict[int, str]:
    from sqlalchemy import select

    from db_utils import run_db_query_with_retry
    from models import Motoboy, User

    if not motoboy_ids:
        return {}
    rows_motoboy = []
    for lote in _chunked(sorted(set(int(m) for m in motoboy_ids)), MAX_IDS_MOTOBOY):
        rows_lote = run_db_query_with_retry(
            db,
            lambda lote=lote: db.execute(
                select(Motoboy.id_motoboy, Motoboy.user_id).where(Motoboy.id_motoboy.in_(lote))
            ).all(),
        )
        rows_motoboy.extend(rows_lote)
    motoboy_user_map = {
        int(mid): (int(uid) if uid is not None else None) for mid, uid in rows_motoboy
    }
    user_ids = sorted({uid for uid in motoboy_user_map.values() if uid is not None})
    user_map: Dict[int, tuple] = {}
    if user_ids:
        rows_user = []
        for lote in _chunked(user_ids, MAX_IDS_MOTOBOY):
            rows_lote = run_db_query_with_retry(
                db,
                lambda lote=lote: db.execute(
                    select(User.id, User.nome, User.sobrenome, User.username).where(User.id.in_(lote))
                ).all(),
            )
            rows_user.extend(rows_lote)
        user_map = {
            int(uid): ((nome or ""), (sobrenome or ""), (username or ""))
            for uid, nome, sobrenome, username in rows_user
        }

    out: Dict[int, str] = {}
    for mid, uid in motoboy_user_map.items():
        if uid is None:
            out[mid] = f"Motoboy {mid}"
            continue
        nome, sobrenome, username_val = user_map.get(uid, ("", "", ""))
        out[mid] = f"{nome} {sobrenome}".strip() or username_val or f"Motoboy {mid}"
    return out


def _build_candidate_stmt(
    sub_base: str,
    de: Optional[date],
    ate: Optional[date],
    base: Optional[str],
    status_: Optional[List[str]],
    servico: Optional[List[str]],
    somente_g: Optional[bool],
    codigo: Optional[str],
    codigo_exato: bool,
    localizar: Optional[str],
):
    from sqlalchemy import exists, func, or_, select

    from models import Saida, SaidaHistorico

    stmt = select(
        Saida.id_saida,
        Saida.timestamp,
        Saida.sub_base,
        Saida.username,
        Saida.entregador,
        Saida.entregador_id,
        Saida.motoboy_id,
        Saida.codigo,
        Saida.servico,
        Saida.status,
        Saida.base,
        Saida.is_grande,
    ).where(Saida.sub_base == sub_base)

    dt_inicio = datetime.combine(de, datetime.min.time()) if de is not None else None
    dt_fim_exclusivo = (
        datetime.combine(ate + timedelta(days=1), datetime.min.time()) if ate is not None else None
    )
    eventos_operacionais = tuple(EVENTOS_ATRIBUICAO_VALIDOS)

    if de is not None:
        if dt_fim_exclusivo is not None:
            subq_hist_periodo = select(1).where(
                SaidaHistorico.id_saida == Saida.id_saida,
                SaidaHistorico.evento.in_(eventos_operacionais),
                SaidaHistorico.timestamp >= dt_inicio,
                SaidaHistorico.timestamp < dt_fim_exclusivo,
            )
            stmt = stmt.where(
                ((Saida.timestamp >= dt_inicio) & (Saida.timestamp < dt_fim_exclusivo))
                | exists(subq_hist_periodo)
            )
        else:
            stmt = stmt.where(
                (Saida.timestamp >= dt_inicio)
                | exists(
                    select(1).where(
                        SaidaHistorico.id_saida == Saida.id_saida,
                        SaidaHistorico.evento.in_(eventos_operacionais),
                        SaidaHistorico.timestamp >= dt_inicio,
                    )
                )
            )
    elif ate is not None:
        subq_hist_ate = select(1).where(
            SaidaHistorico.id_saida == Saida.id_saida,
            SaidaHistorico.evento.in_(eventos_operacionais),
            SaidaHistorico.timestamp < dt_fim_exclusivo,
        )
        stmt = stmt.where((Saida.timestamp < dt_fim_exclusivo) | exists(subq_hist_ate))

    if base and base.strip() and base.lower() != "(todas)":
        base_norm = base.strip().lower()
        stmt = stmt.where(func.unaccent(func.lower(Saida.base)) == func.unaccent(base_norm))

    status_tokens_raw = [
        t for t in _parse_multi_values(status_) if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    status_aliases = sorted(
        {alias for token in status_tokens_raw for alias in _status_group_aliases(token)}
    )
    if status_aliases:
        conds_status = [
            func.unaccent(func.lower(Saida.status)) == func.unaccent(alias) for alias in status_aliases
        ]
        stmt = stmt.where(or_(*conds_status))

    servico_tokens = [
        _norm_text(t)
        for t in _parse_multi_values(servico)
        if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]
    if servico_tokens:
        conds_srv = []
        for srv_norm in servico_tokens:
            if srv_norm == "shopee":
                conds_srv.append(_servico_is_shopee_expr(Saida.servico))
            elif srv_norm in ("mercado livre", "mercadolivre", "mercado_livre", "mercado", "ml", "flex"):
                conds_srv.append(_servico_is_mercado_expr(Saida.servico))
            elif srv_norm == "avulso":
                conds_srv.append((~_servico_is_shopee_expr(Saida.servico)) & (~_servico_is_mercado_expr(Saida.servico)))
            else:
                conds_srv.append(func.unaccent(func.lower(Saida.servico)) == func.unaccent(srv_norm))
        if conds_srv:
            stmt = stmt.where(or_(*conds_srv))

    if somente_g:
        stmt = stmt.where(Saida.is_grande.is_(True))

    if codigo and codigo.strip():
        codigo_trim = codigo.strip().upper()
        if codigo_exato:
            stmt = stmt.where(Saida.codigo == codigo_trim)
        else:
            stmt = stmt.where(or_(Saida.codigo == codigo_trim, Saida.codigo.ilike(f"{codigo_trim}%")))
    elif localizar and localizar.strip():
        q = f"%{localizar.strip()}%"
        stmt = stmt.where(
            or_(
                Saida.base.ilike(q),
                Saida.username.ilike(q),
                Saida.entregador.ilike(q),
                Saida.codigo.ilike(q),
                Saida.servico.ilike(q),
                Saida.status.ilike(q),
            )
        )

    return stmt


def listar_saidas_paginado(
    db,
    *,
    sub_base: str,
    de: Optional[date] = None,
    ate: Optional[date] = None,
    base: Optional[str] = None,
    entregador: Optional[str] = None,
    status_: Optional[List[str]] = None,
    codigo: Optional[str] = None,
    servico: Optional[List[str]] = None,
    acao: Optional[List[str]] = None,
    localizar: Optional[str] = None,
    somente_g: Optional[bool] = None,
    codigo_exato: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
    montar_item,
) -> Dict[str, Any]:
    """Lista saídas com paginação determinística e totalizadores no mesmo conjunto lógico."""
    limit = clamp_listar_limit(limit)
    offset = max(0, int(offset or 0))

    stmt = _build_candidate_stmt(
        sub_base=sub_base,
        de=de,
        ate=ate,
        base=base,
        status_=status_,
        servico=servico,
        somente_g=somente_g,
        codigo=codigo,
        codigo_exato=codigo_exato,
        localizar=localizar,
    )

    from db_utils import run_db_query_with_retry

    raw_rows = run_db_query_with_retry(db, lambda: db.execute(stmt).all())
    candidates = [
        SaidaListRow(
            id_saida=int(r.id_saida),
            timestamp=r.timestamp,
            sub_base=r.sub_base,
            username=r.username,
            entregador=r.entregador,
            entregador_id=r.entregador_id,
            motoboy_id=r.motoboy_id,
            codigo=r.codigo,
            servico=r.servico,
            status=r.status,
            base=r.base,
            is_grande=bool(r.is_grande or False),
        )
        for r in raw_rows
    ]

    ids = [c.id_saida for c in candidates]
    historicos = _load_historico_tuples(db, ids)
    user_ids = [
        int(getattr(h, "user_id"))
        for h in historicos
        if getattr(h, "user_id", None) is not None
    ]
    user_map = _load_user_map(db, user_ids)
    ctx_map = build_operacional_ctx_from_historico_rows(ids, historicos, user_map)

    entregador_filter_norm = ""
    if entregador and entregador.strip() and entregador.lower() != "(todos)":
        entregador_filter_norm = _norm_text(entregador)

    # Nomes de executor: apenas quando filtro de entregador exige comparação por nome de motoboy.
    executor_nome_map: Dict[int, Optional[str]] = {}
    if entregador_filter_norm:
        motoboy_ids = [int(c.motoboy_id) for c in candidates if c.motoboy_id is not None]
        motoboy_nome_map = _load_motoboy_nome_map(db, motoboy_ids)
        for c in candidates:
            if c.motoboy_id is not None:
                executor_nome_map[c.id_saida] = motoboy_nome_map.get(int(c.motoboy_id)) or c.entregador
            else:
                executor_nome_map[c.id_saida] = c.entregador

    acao_tokens = [
        _norm_text(t).replace("_", " ")
        for t in _parse_multi_values(acao)
        if _norm_text(t) not in {"", "(todos)", "todos", "all"}
    ]

    page_rows, totals, ctx_map = filtrar_ordenar_agregar_listagem(
        candidates,
        ctx_map,
        de=de,
        ate=ate,
        entregador_filter_norm=entregador_filter_norm,
        executor_nome_map=executor_nome_map,
        acao_tokens=acao_tokens or None,
        limit=limit,
        offset=offset,
    )

    # Enriquecimento de nomes apenas da página.
    page_motoboy_ids = [int(r.motoboy_id) for r in page_rows if r.motoboy_id is not None]
    page_motoboy_map = _load_motoboy_nome_map(db, page_motoboy_ids)
    page_user_ids = []
    for r in page_rows:
        ctx = ctx_map.get(r.id_saida)
        if ctx and ctx.ultimo_ator_user_id is not None:
            page_user_ids.append(int(ctx.ultimo_ator_user_id))
    # executado_por já veio do user_map completo do histórico; garantir usernames da página
    # (já resolvidos em ctx). Completar nomes de motoboy na página.
    items = []
    for r in page_rows:
        ctx = ctx_map.get(r.id_saida)
        if r.motoboy_id is not None:
            nome_exec = page_motoboy_map.get(int(r.motoboy_id)) or r.entregador
        else:
            nome_exec = r.entregador
        # Adaptador mínimo compatível com _montar_item_listar_saida (atributos de Saida)
        items.append(montar_item(r, ctx, nome_exec))

    return {
        "total": totals["total"],
        "sumShopee": totals["sumShopee"],
        "sumMercado": totals["sumMercado"],
        "sumAvulso": totals["sumAvulso"],
        "items": items,
    }
