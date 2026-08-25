"""Consulta e remoção segura de leituras de coleta operacional."""

from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from coleta_operacional_service import atualizar_status_execucao, resolver_base
from models import (
    BaseFechamento,
    Coleta,
    ColetaExecucao,
    ColetaExecucaoParticipante,
    ColetaLeituraRemocao,
    EntregadorFechamento,
    OwnerCobrancaItem,
    Saida,
    SaidaHistorico,
    User,
)

ROOT_ADMIN_ROLES = {0, 1}

SITUACAO_LABEL = {
    "coletado": "Coletado",
    "aguardando_coleta": "Aguardando coleta",
    "não coletado": "Não coletado",
    "nao coletado": "Não coletado",
    "saiu": "Saiu para entrega",
    "saiu_para_entrega": "Saiu para entrega",
    "em_rota": "Em rota",
    "entregue": "Entregue",
    "ausente": "Ausente",
    "cancelado": "Cancelado",
    "encerrado": "Encerrado",
    "encerrado_sistema": "Encerrado",
    "na_base": "Na base",
}


def _root_admin(user: User) -> bool:
    try:
        return int(user.role) in ROOT_ADMIN_ROLES
    except (TypeError, ValueError):
        return False


def _normalize_servico_key(raw: Optional[str]) -> str:
    s = (raw or "").strip().lower().replace("_", " ")
    if s == "shopee":
        return "shopee"
    if s.startswith("mercado"):
        return "mercado_livre"
    return "avulso"


def _situacao_amigavel(status: Optional[str]) -> str:
    key = (status or "").strip().lower()
    return SITUACAO_LABEL.get(key, (status or "Desconhecido").strip() or "Desconhecido")


def encode_cursor(ts: datetime, id_saida: int) -> str:
    raw = f"{ts.isoformat()}|{id_saida}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.rsplit("|", 1)
        return datetime.fromisoformat(ts_str), int(id_str)
    except Exception as exc:
        raise HTTPException(422, "Cursor de paginação inválido.") from exc


def totais_da_execucao(execucao: Optional[ColetaExecucao]) -> dict[str, int]:
    if not execucao:
        return {"total": 0, "shopee": 0, "mercado_livre": 0, "avulso": 0}
    shopee = sum(int(p.shopee or 0) for p in execucao.participantes)
    mercado_livre = sum(int(p.mercado_livre or 0) for p in execucao.participantes)
    avulso = sum(int(p.avulso or 0) for p in execucao.participantes)
    return {
        "total": shopee + mercado_livre + avulso,
        "shopee": shopee,
        "mercado_livre": mercado_livre,
        "avulso": avulso,
    }


def obter_totais_base_dia(
    db: Session,
    *,
    sub_base: str,
    base_id: int,
    data_operacao: date,
) -> dict[str, int]:
    execucao = db.scalar(
        select(ColetaExecucao).where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.base_id == base_id,
            ColetaExecucao.data_operacao == data_operacao,
        )
    )
    return totais_da_execucao(execucao)


def obter_totais_por_nome_base(
    db: Session,
    *,
    sub_base: str,
    base_nome: str,
    data_operacao: date,
) -> dict[str, int]:
    try:
        base = resolver_base(db, sub_base, nome=base_nome)
    except HTTPException:
        return {"total": 0, "shopee": 0, "mercado_livre": 0, "avulso": 0}
    return obter_totais_base_dia(
        db, sub_base=sub_base, base_id=base.id_base, data_operacao=data_operacao
    )


def resumo_base_dia(
    db: Session,
    *,
    sub_base: str,
    base_id: int,
    data_operacao: date,
) -> dict[str, Any]:
    base = resolver_base(db, sub_base, base_id=base_id)
    execucao = db.scalar(
        select(ColetaExecucao).where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.base_id == base.id_base,
            ColetaExecucao.data_operacao == data_operacao,
        )
    )
    totais = totais_da_execucao(execucao)
    return {
        "base_id": base.id_base,
        "base": base.base,
        "data_operacao": data_operacao,
        "status": execucao.status if execucao else "pendente",
        "id_execucao": execucao.id_execucao if execucao else None,
        "total": totais["total"],
        "shopee": totais["shopee"],
        "mercado_livre": totais["mercado_livre"],
        "avulso": totais["avulso"],
        "atualizado_em": execucao.atualizado_em if execucao else None,
    }


def _dono_user_id(db: Session, saida: Saida) -> Optional[int]:
    hist = db.scalar(
        select(SaidaHistorico)
        .where(
            SaidaHistorico.id_saida == saida.id_saida,
            SaidaHistorico.evento == "criado_coleta",
        )
        .order_by(SaidaHistorico.id.asc())
        .limit(1)
    )
    if hist and hist.user_id:
        return int(hist.user_id)
    if saida.id_coleta:
        coleta = db.get(Coleta, saida.id_coleta)
        if coleta and coleta.participante_id:
            part = db.get(ColetaExecucaoParticipante, coleta.participante_id)
            if part:
                return int(part.user_id)
    return None


def _dono_username(db: Session, saida: Saida, dono_user_id: Optional[int]) -> str:
    if dono_user_id:
        user = db.get(User, dono_user_id)
        if user and user.username:
            return user.username
    return (saida.username or "").strip() or "-"


def _garantir_nao_fechado(
    db: Session,
    *,
    sub_base: str,
    base_nome: str,
    data_operacao: date,
    motoboy_id: Optional[int],
) -> None:
    fechamento_base = db.scalar(
        select(BaseFechamento.id_fechamento).where(
            BaseFechamento.sub_base == sub_base,
            func.upper(BaseFechamento.base) == base_nome.upper(),
            BaseFechamento.periodo_inicio <= data_operacao,
            BaseFechamento.periodo_fim >= data_operacao,
        )
    )
    fechamento_motoboy = None
    if motoboy_id:
        fechamento_motoboy = db.scalar(
            select(EntregadorFechamento.id_fechamento).where(
                EntregadorFechamento.sub_base == sub_base,
                EntregadorFechamento.id_motoboy == motoboy_id,
                EntregadorFechamento.periodo_inicio <= data_operacao,
                EntregadorFechamento.periodo_fim >= data_operacao,
            )
        )
    if fechamento_base or fechamento_motoboy:
        raise HTTPException(
            409,
            "A coleta pertence a um período com fechamento gerado e não pode mais ser alterada.",
        )


def avaliar_remocao(
    db: Session,
    *,
    saida: Saida,
    current_user: User,
    data_operacao: Optional[date] = None,
) -> tuple[bool, Optional[str], Optional[int]]:
    """Retorna (pode_remover, motivo_bloqueio, dono_user_id)."""
    dono_id = _dono_user_id(db, saida)

    if not saida.id_coleta:
        return False, "Não é uma leitura de coleta.", dono_id

    coleta = db.get(Coleta, saida.id_coleta)
    if not coleta or (coleta.origem or "codigo") == "manual":
        return False, "Lançamento manual não pode ser removido por este fluxo.", dono_id

    status = (saida.status or "").strip().lower()
    if status != "coletado":
        return False, f"Pacote já está em fluxo posterior ({_situacao_amigavel(saida.status)}).", dono_id

    if saida.motoboy_id:
        return False, "Pacote já foi atribuído a um entregador.", dono_id

    dia = data_operacao or (saida.data if saida.data else (saida.timestamp.date() if saida.timestamp else date.today()))
    if saida.data and saida.data != dia:
        return False, "Leitura fora do dia operacional selecionado.", dono_id

    cobranca = db.scalar(
        select(OwnerCobrancaItem).where(OwnerCobrancaItem.id_saida == saida.id_saida)
    )
    if cobranca and bool(cobranca.fechado):
        return False, "Cobrança já fechada para este pacote.", dono_id

    # Eventos posteriores ao criado_coleta indicam progresso operacional
    eventos_posteriores = db.scalar(
        select(func.count())
        .select_from(SaidaHistorico)
        .where(
            SaidaHistorico.id_saida == saida.id_saida,
            SaidaHistorico.evento.notin_(["criado_coleta"]),
        )
    )
    if eventos_posteriores and int(eventos_posteriores) > 0:
        return False, "Pacote já possui eventos posteriores à coleta.", dono_id

    is_owner = dono_id is not None and dono_id == current_user.id
    if not is_owner and not _root_admin(current_user):
        # fallback username se não houver user_id no histórico
        if dono_id is None:
            uname = (saida.username or "").strip().lower()
            if uname and uname == (current_user.username or "").strip().lower():
                is_owner = True
        if not is_owner:
            return False, "Somente o operador da leitura ou admin pode remover.", dono_id

    return True, None, dono_id


def listar_leituras(
    db: Session,
    *,
    sub_base: str,
    current_user: User,
    base_id: int,
    data_operacao: date,
    limit: int = 40,
    cursor: Optional[str] = None,
    somente_minhas: bool = False,
) -> dict[str, Any]:
    base = resolver_base(db, sub_base, base_id=base_id)
    limit = max(1, min(int(limit or 40), 100))

    stmt = (
        select(Saida)
        .where(
            Saida.sub_base == sub_base,
            func.upper(Saida.base) == (base.base or "").strip().upper(),
            Saida.data == data_operacao,
            Saida.id_coleta.is_not(None),
            Saida.codigo.is_not(None),
            func.length(func.trim(Saida.codigo)) > 0,
        )
        .order_by(Saida.timestamp.desc(), Saida.id_saida.desc())
        .limit(limit + 1)
    )

    if cursor:
        cursor_ts, cursor_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Saida.timestamp < cursor_ts,
                and_(Saida.timestamp == cursor_ts, Saida.id_saida < cursor_id),
            )
        )

    if somente_minhas:
        hist_subq = select(SaidaHistorico.id_saida).where(
            SaidaHistorico.evento == "criado_coleta",
            SaidaHistorico.user_id == current_user.id,
        )
        stmt = stmt.where(
            or_(
                Saida.id_saida.in_(hist_subq),
                func.lower(Saida.username) == (current_user.username or "").strip().lower(),
            )
        )

    rows = list(db.scalars(stmt).all())
    has_more = len(rows) > limit
    page = rows[:limit]

    itens = []
    next_cursor = None
    for saida in page:
        pode, motivo, dono_id = avaliar_remocao(
            db, saida=saida, current_user=current_user, data_operacao=data_operacao
        )
        itens.append(
            {
                "id_saida": saida.id_saida,
                "codigo": saida.codigo,
                "servico": saida.servico,
                "horario": saida.timestamp,
                "operador": _dono_username(db, saida, dono_id),
                "operador_user_id": dono_id,
                "situacao": _situacao_amigavel(saida.status),
                "status": saida.status,
                "pode_remover": pode,
                "motivo_bloqueio": motivo,
            }
        )
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(last.timestamp, last.id_saida)

    return {
        "base_id": base.id_base,
        "base": base.base,
        "data_operacao": data_operacao,
        "itens": itens,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _recalcular_coleta_sem_commit(db: Session, coleta: Coleta) -> Optional[Coleta]:
    saidas = list(db.scalars(select(Saida).where(Saida.id_coleta == coleta.id_coleta)).all())
    if not saidas:
        db.delete(coleta)
        db.flush()
        return None

    count = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
    pacotes_g = 0
    g_shopee = g_ml = g_avulso = 0
    for s in saidas:
        key = _normalize_servico_key(s.servico)
        count[key] += 1
        if bool(getattr(s, "is_grande", False)):
            pacotes_g += 1
            if key == "shopee":
                g_shopee += 1
            elif key == "mercado_livre":
                g_ml += 1
            else:
                g_avulso += 1

    from coletas import _get_precos_cached, _decimal

    p_shopee, p_ml, p_avulso = _get_precos_cached(db, coleta.sub_base, coleta.base)
    total = (
        _decimal(count["shopee"]) * p_shopee
        + _decimal(count["mercado_livre"]) * p_ml
        + _decimal(count["avulso"]) * p_avulso
    ).quantize(Decimal("0.01"))

    coleta.shopee = count["shopee"]
    coleta.mercado_livre = count["mercado_livre"]
    coleta.avulso = count["avulso"]
    coleta.pacotes_g = pacotes_g
    coleta.g_shopee = g_shopee
    coleta.g_ml = g_ml
    coleta.g_avulso = g_avulso
    coleta.valor_total = total
    return coleta


def _decrementar_participante(
    db: Session,
    *,
    coleta: Coleta,
    servico_key: str,
    is_grande: bool,
) -> Optional[ColetaExecucao]:
    participante = None
    if coleta.participante_id:
        participante = db.get(ColetaExecucaoParticipante, coleta.participante_id)
    if not participante and coleta.execucao_id:
        # fallback: não decrementa se não achar participante
        return db.get(ColetaExecucao, coleta.execucao_id)

    if not participante:
        return None

    if servico_key == "shopee":
        participante.shopee = max(0, int(participante.shopee or 0) - 1)
        if is_grande:
            participante.g_shopee = max(0, int(participante.g_shopee or 0) - 1)
    elif servico_key == "mercado_livre":
        participante.mercado_livre = max(0, int(participante.mercado_livre or 0) - 1)
        if is_grande:
            participante.g_ml = max(0, int(participante.g_ml or 0) - 1)
    else:
        participante.avulso = max(0, int(participante.avulso or 0) - 1)
        if is_grande:
            participante.g_avulso = max(0, int(participante.g_avulso or 0) - 1)

    if is_grande:
        participante.pacotes_g = max(0, int(participante.pacotes_g or 0) - 1)

    total = (
        int(participante.shopee or 0)
        + int(participante.mercado_livre or 0)
        + int(participante.avulso or 0)
    )
    participante.sem_volume = total == 0
    participante.versao = int(participante.versao or 1) + 1
    participante.atualizado_em = datetime.now()

    execucao = db.get(ColetaExecucao, participante.execucao_id)
    if execucao:
        atualizar_status_execucao(execucao)
    return execucao


def remover_leitura(
    db: Session,
    *,
    sub_base: str,
    current_user: User,
    id_saida: int,
    motivo: Optional[str] = None,
) -> dict[str, Any]:
    # Idempotência: já removida
    audit = db.scalar(
        select(ColetaLeituraRemocao)
        .where(
            ColetaLeituraRemocao.sub_base == sub_base,
            ColetaLeituraRemocao.id_saida == id_saida,
        )
        .order_by(ColetaLeituraRemocao.id.desc())
        .limit(1)
    )
    saida = db.scalar(
        select(Saida)
        .where(Saida.id_saida == id_saida, Saida.sub_base == sub_base)
        .with_for_update()
    )
    if not saida:
        if audit:
            totais = obter_totais_base_dia(
                db,
                sub_base=sub_base,
                base_id=int(audit.base_id or 0),
                data_operacao=audit.data_operacao,
            ) if audit.base_id else obter_totais_por_nome_base(
                db,
                sub_base=sub_base,
                base_nome=audit.base,
                data_operacao=audit.data_operacao,
            )
            return {
                "removido": True,
                "id_saida": id_saida,
                "codigo": audit.codigo,
                "totais": totais,
                "idempotente": True,
            }
        raise HTTPException(404, "Leitura não encontrada nesta sub_base.")

    pode, motivo_bloqueio, dono_id = avaliar_remocao(db, saida=saida, current_user=current_user)
    if not pode:
        raise HTTPException(409, motivo_bloqueio or "Leitura não pode ser removida.")

    coleta = db.get(Coleta, saida.id_coleta)
    if not coleta:
        raise HTTPException(409, "Coleta vinculada não encontrada.")

    data_op = saida.data or (saida.timestamp.date() if saida.timestamp else date.today())
    base_nome = (saida.base or coleta.base or "").strip()
    motoboy_id = None
    if coleta.participante_id:
        part = db.get(ColetaExecucaoParticipante, coleta.participante_id)
        if part:
            motoboy_id = part.motoboy_id

    _garantir_nao_fechado(
        db,
        sub_base=sub_base,
        base_nome=base_nome,
        data_operacao=data_op,
        motoboy_id=motoboy_id,
    )

    try:
        base_ref = resolver_base(db, sub_base, nome=base_nome)
        base_id = base_ref.id_base
    except HTTPException:
        base_id = None

    servico_key = _normalize_servico_key(saida.servico)
    is_grande = bool(getattr(saida, "is_grande", False))
    codigo = saida.codigo or ""
    operador_username = _dono_username(db, saida, dono_id)

    # Auditoria antes de apagar
    db.add(
        ColetaLeituraRemocao(
            sub_base=sub_base,
            base_id=base_id,
            base=base_nome,
            data_operacao=data_op,
            id_saida=id_saida,
            codigo=codigo,
            servico=saida.servico,
            operador_user_id=dono_id,
            operador_username=operador_username,
            removido_por_user_id=current_user.id,
            removido_por_username=current_user.username or "-",
            motivo=(motivo or "Remoção operacional de leitura de coleta").strip()[:500],
        )
    )

    # Cancelar cobrança
    for item in db.scalars(
        select(OwnerCobrancaItem).where(OwnerCobrancaItem.id_saida == id_saida)
    ).all():
        item.cancelado = True

    # Remover histórico e saída
    db.execute(delete(SaidaHistorico).where(SaidaHistorico.id_saida == id_saida))
    db.delete(saida)
    db.flush()

    # Decrementar participante antes de eventualmente apagar coleta
    _decrementar_participante(
        db, coleta=coleta, servico_key=servico_key, is_grande=is_grande
    )
    _recalcular_coleta_sem_commit(db, coleta)

    db.commit()

    totais = (
        obter_totais_base_dia(db, sub_base=sub_base, base_id=base_id, data_operacao=data_op)
        if base_id
        else obter_totais_por_nome_base(
            db, sub_base=sub_base, base_nome=base_nome, data_operacao=data_op
        )
    )
    return {
        "removido": True,
        "id_saida": id_saida,
        "codigo": codigo,
        "totais": totais,
        "idempotente": False,
    }
