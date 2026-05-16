from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import SaidaHistorico, User

EVENTOS_ATRIBUICAO_VALIDOS = {
    "lido",
    "scan",
    "assumir",
    "assumido",
    "reatribuicao",
    "reatribuido",
}

EVENTOS_INVALIDANTES = {
    "removido_sem_inicio",
    "desatribuido",
}

ROTULOS_ACAO = {
    "lido": "Lido",
    "scan": "Scan",
    "assumir": "Reatribuido",
    "assumido": "Reatribuido",
    "reatribuicao": "Reatribuido",
    "reatribuido": "Reatribuido",
    "removido_sem_inicio": "Removido sem iniciar rota",
    "em_rota": "Iniciou rota",
    "entregue": "Entregue",
    "ausente": "Ausente",
    "cancelado": "Cancelado",
    "desatribuido": "Desatribuido",
}


@dataclass
class SaidaOperacionalContext:
    id_saida: int
    ultimo_evento: Optional[str]
    ultimo_evento_ts: Optional[datetime]
    acao_label: Optional[str]
    ultimo_ator_username: Optional[str]
    ultimo_ator_user_id: Optional[int]
    operacional_evento: Optional[str]
    operacional_ts: Optional[datetime]
    leitura_valida: bool
    removido_sem_inicio_ativo: bool


def _rotulo_acao(evento: Optional[str]) -> Optional[str]:
    if not evento:
        return None
    key = (evento or "").strip().lower()
    return ROTULOS_ACAO.get(key, (evento or "").replace("_", " ").strip().capitalize())


def carregar_contexto_operacional(
    db: Session,
    saida_ids: Iterable[int],
) -> Dict[int, SaidaOperacionalContext]:
    ids = [int(i) for i in saida_ids if i is not None]
    if not ids:
        return {}

    historicos = db.execute(
        select(SaidaHistorico)
        .where(SaidaHistorico.id_saida.in_(ids))
        .order_by(SaidaHistorico.id_saida.asc(), SaidaHistorico.timestamp.asc(), SaidaHistorico.id.asc())
    ).scalars().all()

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
            },
        )

        estado["ultimo"] = h
        if h.user_id is not None:
            user_ids.add(int(h.user_id))

        if evento in EVENTOS_INVALIDANTES:
            estado["op"] = None
            estado["removido_ativo"] = True
            continue

        if evento in EVENTOS_ATRIBUICAO_VALIDOS:
            estado["op"] = h
            estado["removido_ativo"] = False

    user_map: Dict[int, str] = {}
    if user_ids:
        rows_user = db.execute(
            select(User.id, User.username).where(User.id.in_(sorted(user_ids)))
        ).all()
        user_map = {int(uid): (uname or "") for uid, uname in rows_user}

    out: Dict[int, SaidaOperacionalContext] = {}
    for sid in ids:
        estado = estado_por_saida.get(sid)
        if not estado:
            out[sid] = SaidaOperacionalContext(
                id_saida=sid,
                ultimo_evento=None,
                ultimo_evento_ts=None,
                acao_label=None,
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
        op_evento = (getattr(op, "evento", None) or None) if op is not None else None
        op_user_id = getattr(op, "user_id", None) if op is not None else None
        op_user_id = int(op_user_id) if op_user_id is not None else None

        out[sid] = SaidaOperacionalContext(
            id_saida=sid,
            ultimo_evento=ultimo_evento,
            ultimo_evento_ts=getattr(ultimo, "timestamp", None) if ultimo is not None else None,
            acao_label=_rotulo_acao(ultimo_evento),
            ultimo_ator_username=user_map.get(op_user_id) if op_user_id is not None else None,
            ultimo_ator_user_id=op_user_id,
            operacional_evento=op_evento,
            operacional_ts=getattr(op, "timestamp", None) if op is not None else None,
            leitura_valida=op is not None,
            removido_sem_inicio_ativo=bool(estado.get("removido_ativo", False)),
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
        if ctx and (ctx.removido_sem_inicio_ativo or not ctx.leitura_valida):
            continue
        ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or getattr(s, "timestamp", None)
        if ts is None:
            continue
        dia = ts.date()
        if periodo_inicio is not None and dia < periodo_inicio:
            continue
        if periodo_fim is not None and dia > periodo_fim:
            continue
        filtradas.append(s)
    return filtradas, ctx_map
