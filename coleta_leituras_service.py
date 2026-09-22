"""Consulta e remoção segura de leituras de coleta operacional."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from coleta_operacional_service import (
    atualizar_status_execucao,
    obter_ou_criar_execucao,
    resolver_base,
    resolver_executor,
)
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
from saidas_listar_service import invalidate_listar_cache

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


def _decimal(v) -> Decimal:
    return Decimal(str(v or 0))


def _recalcular_coleta_sem_commit(db: Session, coleta: Coleta) -> Optional[Coleta]:
    saidas = list(db.scalars(select(Saida).where(Saida.id_coleta == coleta.id_coleta)).all())
    if not saidas:
        # Evita FK quebrada ao apagar execução depois.
        coleta.execucao_id = None
        coleta.participante_id = None
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

    # Case-insensitive (mesmo critério de resolver_base); evita import circular de coletas.
    try:
        base_ref = resolver_base(db, coleta.sub_base or "", nome=coleta.base or "")
        p_shopee = _decimal(base_ref.shopee)
        p_ml = _decimal(base_ref.ml)
        p_avulso = _decimal(base_ref.avulso)
    except HTTPException:
        p_shopee = p_ml = p_avulso = Decimal("0.00")

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


def _quantidade_participante(participante: ColetaExecucaoParticipante) -> int:
    return (
        int(participante.shopee or 0)
        + int(participante.mercado_livre or 0)
        + int(participante.avulso or 0)
    )


def _aplicar_delta_servico(
    participante: ColetaExecucaoParticipante,
    *,
    servico_key: str,
    is_grande: bool,
    delta: int,
) -> None:
    """Aplica +1/-1 em contadores do participante por serviço."""
    if delta == 0:
        return
    if servico_key == "shopee":
        participante.shopee = max(0, int(participante.shopee or 0) + delta)
        if is_grande:
            participante.g_shopee = max(0, int(participante.g_shopee or 0) + delta)
    elif servico_key == "mercado_livre":
        participante.mercado_livre = max(0, int(participante.mercado_livre or 0) + delta)
        if is_grande:
            participante.g_ml = max(0, int(participante.g_ml or 0) + delta)
    else:
        participante.avulso = max(0, int(participante.avulso or 0) + delta)
        if is_grande:
            participante.g_avulso = max(0, int(participante.g_avulso or 0) + delta)
    if is_grande:
        participante.pacotes_g = max(0, int(participante.pacotes_g or 0) + delta)


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

    _aplicar_delta_servico(
        participante, servico_key=servico_key, is_grande=is_grande, delta=-1
    )

    total = _quantidade_participante(participante)
    participante.sem_volume = total == 0
    participante.versao = int(participante.versao or 1) + 1
    participante.atualizado_em = datetime.now()

    execucao = db.get(ColetaExecucao, participante.execucao_id)
    if execucao:
        atualizar_status_execucao(execucao)
    return execucao


def _obter_ou_criar_participante_destino(
    db: Session,
    *,
    execucao: ColetaExecucao,
    sub_base: str,
    current_user: User,
) -> ColetaExecucaoParticipante:
    executor, motoboy_id = resolver_executor(db, current_user)
    participante = db.scalar(
        select(ColetaExecucaoParticipante).where(
            ColetaExecucaoParticipante.execucao_id == execucao.id_execucao,
            ColetaExecucaoParticipante.user_id == executor.id,
        )
    )
    if participante:
        return participante
    participante = ColetaExecucaoParticipante(
        execucao_id=execucao.id_execucao,
        sub_base=sub_base,
        user_id=executor.id,
        motoboy_id=motoboy_id,
        username=executor.username or current_user.username or "-",
        shopee=0,
        mercado_livre=0,
        avulso=0,
        pacotes_g=0,
        status="finalizado",
        atualizado_por_user_id=current_user.id,
    )
    db.add(participante)
    db.flush()
    return participante


def _incrementar_participante(
    db: Session,
    *,
    participante: ColetaExecucaoParticipante,
    servico_key: str,
    is_grande: bool,
    current_user: User,
) -> ColetaExecucao:
    _aplicar_delta_servico(
        participante, servico_key=servico_key, is_grande=is_grande, delta=1
    )
    participante.sem_volume = False
    participante.status = "finalizado"
    participante.versao = int(participante.versao or 1) + 1
    participante.atualizado_em = datetime.now()
    participante.atualizado_por_user_id = current_user.id
    execucao = db.get(ColetaExecucao, participante.execucao_id)
    if not execucao:
        raise HTTPException(500, "Execução de destino não encontrada.")
    if execucao.status not in ("coletado", "sem_volume", "em_coleta"):
        execucao.status = "em_coleta"
    atualizar_status_execucao(execucao)
    return execucao


def _limpar_execucao_sem_volume(db: Session, execucao: Optional[ColetaExecucao]) -> None:
    """Remove participantes zerados; se ninguém restar, apaga a execução (volta a Pendente)."""
    if not execucao or not getattr(execucao, "id_execucao", None):
        return
    execucao_id = execucao.id_execucao
    vivos = list(
        db.scalars(
            select(ColetaExecucaoParticipante).where(
                ColetaExecucaoParticipante.execucao_id == execucao_id
            )
        ).all()
    )
    for part in vivos:
        if _quantidade_participante(part) == 0:
            db.delete(part)
    db.flush()
    restantes = list(
        db.scalars(
            select(ColetaExecucaoParticipante).where(
                ColetaExecucaoParticipante.execucao_id == execucao_id
            )
        ).all()
    )
    atual = db.get(ColetaExecucao, execucao_id)
    if not atual:
        return
    if not restantes:
        # Desvincula coletas remanescentes antes de apagar a execução (evita IntegrityError).
        for coleta in list(
            db.scalars(select(Coleta).where(Coleta.execucao_id == execucao_id)).all()
        ):
            coleta.execucao_id = None
            coleta.participante_id = None
        db.flush()
        db.delete(atual)
        db.flush()
        return
    atualizar_status_execucao(atual)


def transferir_base_coleta(
    db: Session,
    *,
    sub_base: str,
    current_user: User,
    ids_saida: list[int],
    base_destino: str,
    origem_cliente: str = "web",
) -> dict[str, Any]:
    """
    Transfere pacotes de uma base de coleta para outra no mesmo dia operacional.

    Atualiza Saida.base, move o crédito no ledger (ColetaExecucao) e recalcula status:
    origem sem volume → Pendente; destino com volume → Coletada.
    """
    ids_unicos: list[int] = []
    vistos: set[int] = set()
    for raw in ids_saida:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            continue
        if sid in vistos:
            continue
        vistos.add(sid)
        ids_unicos.append(sid)

    if not ids_unicos:
        raise HTTPException(422, "Informe ao menos um pacote para transferir.")

    dest_nome = (base_destino or "").strip()
    if not dest_nome:
        raise HTTPException(422, "Informe a base de destino.")

    base_dest = resolver_base(db, sub_base, nome=dest_nome)

    saidas = list(
        db.scalars(
            select(Saida)
            .where(Saida.sub_base == sub_base, Saida.id_saida.in_(ids_unicos))
            .with_for_update()
        ).all()
    )
    if len(saidas) != len(ids_unicos):
        raise HTTPException(404, "Um ou mais pacotes não foram encontrados nesta sub_base.")

    origens = {(s.base or "").strip() for s in saidas}
    if "" in origens:
        raise HTTPException(422, "Todos os pacotes precisam ter base de origem informada.")
    if len(origens) != 1:
        raise HTTPException(
            422,
            "Para transferir, selecione apenas pacotes da mesma base de origem.",
        )
    base_origem_nome = next(iter(origens))
    if base_origem_nome.strip().upper() == dest_nome.strip().upper():
        raise HTTPException(422, "A base de destino deve ser diferente da origem.")

    if any(not getattr(s, "id_coleta", None) for s in saidas):
        raise HTTPException(
            422,
            "Só é possível transferir pacotes vinculados a uma coleta operacional.",
        )

    dias = {s.data or (s.timestamp.date() if s.timestamp else None) for s in saidas}
    if None in dias or len(dias) != 1:
        raise HTTPException(
            422,
            "Para transferir, selecione apenas pacotes do mesmo dia operacional.",
        )
    data_operacao = next(iter(dias))

    # Dias anteriores: admin/root/operador no painel web (mesmo critério das coletas).
    if data_operacao != date.today():
        try:
            role = int(getattr(current_user, "role", -1))
        except (TypeError, ValueError):
            role = -1
        if role not in {0, 1, 2}:
            raise HTTPException(403, "O usuário pode transferir somente coletas do dia atual.")
        if (origem_cliente or "").strip().lower() != "web":
            raise HTTPException(
                403,
                "Transferências de dias anteriores são permitidas somente no painel web.",
            )

    try:
        base_origem = resolver_base(db, sub_base, nome=base_origem_nome)
    except HTTPException as exc:
        raise HTTPException(
            exc.status_code,
            f"Base de origem '{base_origem_nome}' inválida ou inativa.",
        ) from exc

    _garantir_nao_fechado(
        db,
        sub_base=sub_base,
        base_nome=base_origem.base,
        data_operacao=data_operacao,
        motoboy_id=None,
    )
    _garantir_nao_fechado(
        db,
        sub_base=sub_base,
        base_nome=base_dest.base,
        data_operacao=data_operacao,
        motoboy_id=None,
    )

    contagem = {"shopee": 0, "mercado_livre": 0, "avulso": 0}
    coletas_origem_ids: set[int] = set()
    execucoes_origem: dict[int, ColetaExecucao] = {}

    execucao_dest = obter_ou_criar_execucao(
        db,
        sub_base=sub_base,
        base=base_dest,
        data_operacao=data_operacao,
        modo="codigo",
    )
    participante_dest = _obter_ou_criar_participante_destino(
        db,
        execucao=execucao_dest,
        sub_base=sub_base,
        current_user=current_user,
    )

    coleta_dest = Coleta(
        sub_base=sub_base,
        base=base_dest.base,
        username_entregador=getattr(current_user, "username", None),
        shopee=0,
        mercado_livre=0,
        avulso=0,
        pacotes_g=0,
        valor_total=Decimal("0.00"),
        origem="codigo",
        execucao_id=execucao_dest.id_execucao,
        participante_id=participante_dest.id_participante,
    )
    db.add(coleta_dest)
    db.flush()

    hist_payload = json.dumps(
        {
            "base_origem": base_origem.base,
            "base_destino": base_dest.base,
            "data_operacao": data_operacao.isoformat(),
        },
        ensure_ascii=False,
    )

    for saida in saidas:
        servico_key = _normalize_servico_key(saida.servico)
        is_grande = bool(getattr(saida, "is_grande", False))
        contagem[servico_key] = contagem.get(servico_key, 0) + 1

        coleta_origem = db.get(Coleta, saida.id_coleta) if saida.id_coleta else None
        if coleta_origem:
            coletas_origem_ids.add(coleta_origem.id_coleta)
            execucao = _decrementar_participante(
                db,
                coleta=coleta_origem,
                servico_key=servico_key,
                is_grande=is_grande,
            )
            if execucao and execucao.id_execucao:
                execucoes_origem[execucao.id_execucao] = execucao

        saida.base = base_dest.base
        saida.id_coleta = coleta_dest.id_coleta

        _incrementar_participante(
            db,
            participante=participante_dest,
            servico_key=servico_key,
            is_grande=is_grande,
            current_user=current_user,
        )

        db.add(
            SaidaHistorico(
                id_saida=saida.id_saida,
                evento="base_transferida",
                status_anterior=saida.status,
                status_novo=saida.status,
                user_id=current_user.id,
                payload=hist_payload,
            )
        )

    for id_coleta in coletas_origem_ids:
        coleta = db.get(Coleta, id_coleta)
        if coleta:
            _recalcular_coleta_sem_commit(db, coleta)

    db.flush()
    _recalcular_coleta_sem_commit(db, coleta_dest)

    for execucao in list(execucoes_origem.values()):
        _limpar_execucao_sem_volume(db, execucao)

    execucao_dest_atual = db.get(ColetaExecucao, execucao_dest.id_execucao)
    if execucao_dest_atual:
        atualizar_status_execucao(execucao_dest_atual)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Erro ao transferir base da coleta: {exc}") from exc

    invalidate_listar_cache(sub_base)

    totais_origem = obter_totais_base_dia(
        db,
        sub_base=sub_base,
        base_id=base_origem.id_base,
        data_operacao=data_operacao,
    )
    totais_destino = obter_totais_base_dia(
        db,
        sub_base=sub_base,
        base_id=base_dest.id_base,
        data_operacao=data_operacao,
    )

    status_origem = "pendente" if totais_origem["total"] == 0 else "coletado"
    status_destino = "pendente" if totais_destino["total"] == 0 else "coletado"

    return {
        "transferidos": len(saidas),
        "base_origem": base_origem.base,
        "base_destino": base_dest.base,
        "data_operacao": data_operacao.isoformat(),
        "contagem": contagem,
        "status_origem": status_origem,
        "status_destino": status_destino,
        "totais_origem": totais_origem,
        "totais_destino": totais_destino,
    }


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
