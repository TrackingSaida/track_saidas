from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from auth import get_current_user
from coleta_operacional_service import (
    exigir_modo,
    modo_coleta,
    obter_ou_criar_execucao,
    resolver_base,
    resolver_executor,
)
from db import get_db
from models import (
    BaseFechamento,
    BasePreco,
    Coleta,
    ColetaCalendarioExcecao,
    ColetaExecucao,
    ColetaExecucaoParticipante,
    EntregadorFechamento,
    User,
)

router = APIRouter(prefix="/coletas/operacionais", tags=["Coletas operacionais"])
ADMIN_ROLES = {0, 1, 2}
TIPOS_EXCECAO = {"FERIADO", "SEM_COLETA", "COLETA_EXTRA", "JUSTIFICADO"}


def _sub_base(current_user: User) -> str:
    value = (getattr(current_user, "sub_base", None) or "").strip()
    if not value:
        raise HTTPException(422, "Usuário sem sub_base definida.")
    return value


def _admin(current_user: User) -> bool:
    try:
        return int(current_user.role) in ADMIN_ROLES
    except (TypeError, ValueError):
        return False


def _exigir_coleta_habilitada(db: Session, sub_base: str) -> str:
    modo = modo_coleta(db, sub_base)
    if modo == "desativado":
        raise HTTPException(403, "As coletas estão desativadas para este owner.")
    return modo


def _validar_data_edicao(current_user: User, data_operacao: date, origem_cliente: str) -> None:
    if data_operacao == date.today():
        return
    if not _admin(current_user):
        raise HTTPException(403, "O usuário pode alterar somente coletas do dia atual.")
    if origem_cliente != "web":
        raise HTTPException(403, "Alterações de dias anteriores são permitidas somente no painel web.")


def _quantidade_total(obj) -> int:
    return int(obj.shopee or 0) + int(obj.mercado_livre or 0) + int(obj.avulso or 0)


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


class ContribuicaoManualIn(BaseModel):
    base_id: int
    data_operacao: date = Field(default_factory=date.today)
    executor_user_id: Optional[int] = None
    shopee: int = Field(default=0, ge=0)
    mercado_livre: int = Field(default=0, ge=0)
    avulso: int = Field(default=0, ge=0)
    pacotes_g: int = Field(default=0, ge=0)
    g_shopee: int = Field(default=0, ge=0)
    g_ml: int = Field(default=0, ge=0)
    g_avulso: int = Field(default=0, ge=0)
    sem_volume: bool = False
    client_request_id: Optional[str] = Field(default=None, min_length=8, max_length=100)
    origem_cliente: Literal["web", "mobile"] = "web"

    @model_validator(mode="after")
    def validar_volume(self):
        total = self.shopee + self.mercado_livre + self.avulso
        if self.sem_volume and total:
            raise ValueError("sem_volume não pode ser combinado com quantidades")
        if not self.sem_volume and total == 0:
            raise ValueError("Informe alguma quantidade ou marque sem_volume")
        return self


class ContribuicaoManualUpdate(BaseModel):
    shopee: int = Field(ge=0)
    mercado_livre: int = Field(ge=0)
    avulso: int = Field(ge=0)
    pacotes_g: int = Field(default=0, ge=0)
    g_shopee: int = Field(default=0, ge=0)
    g_ml: int = Field(default=0, ge=0)
    g_avulso: int = Field(default=0, ge=0)
    sem_volume: bool = False
    versao: int = Field(ge=1)
    origem_cliente: Literal["web", "mobile"] = "web"

    @model_validator(mode="after")
    def validar_volume(self):
        total = self.shopee + self.mercado_livre + self.avulso
        if self.sem_volume and total:
            raise ValueError("sem_volume não pode ser combinado com quantidades")
        if not self.sem_volume and total == 0:
            raise ValueError("Informe alguma quantidade ou marque sem_volume")
        return self


class ParticipanteOut(BaseModel):
    id_participante: int
    user_id: int
    motoboy_id: Optional[int]
    username: str
    shopee: int
    mercado_livre: int
    avulso: int
    pacotes_g: int
    g_shopee: int
    g_ml: int
    g_avulso: int
    sem_volume: bool
    versao: int
    total: int
    pode_editar: bool


class ExecucaoOut(BaseModel):
    id_execucao: int
    base_id: int
    base: str
    data_operacao: date
    modo: str
    status: str
    total: int
    shopee: int
    mercado_livre: int
    avulso: int
    participantes: list[ParticipanteOut]


def _serializar_execucao(execucao: ColetaExecucao, current_user: User, participantes) -> ExecucaoOut:
    is_admin = _admin(current_user)
    itens = []
    for p in participantes:
        if not is_admin and p.user_id != current_user.id:
            continue
        itens.append(
            ParticipanteOut(
                id_participante=p.id_participante,
                user_id=p.user_id,
                motoboy_id=p.motoboy_id,
                username=p.username,
                shopee=p.shopee,
                mercado_livre=p.mercado_livre,
                avulso=p.avulso,
                pacotes_g=p.pacotes_g,
                g_shopee=p.g_shopee,
                g_ml=p.g_ml,
                g_avulso=p.g_avulso,
                sem_volume=bool(p.sem_volume),
                versao=p.versao,
                total=_quantidade_total(p),
                pode_editar=(p.user_id == current_user.id and execucao.data_operacao == date.today()) or is_admin,
            )
        )
    return ExecucaoOut(
        id_execucao=execucao.id_execucao,
        base_id=execucao.base_id,
        base=execucao.base_ref.base,
        data_operacao=execucao.data_operacao,
        modo=execucao.modo,
        status=execucao.status,
        total=sum(item.total for item in itens),
        shopee=sum(item.shopee for item in itens),
        mercado_livre=sum(item.mercado_livre for item in itens),
        avulso=sum(item.avulso for item in itens),
        participantes=itens,
    )


def _sincronizar_coleta_legada(db: Session, participante: ColetaExecucaoParticipante, execucao: ColetaExecucao) -> None:
    base = execucao.base_ref
    valor = (
        Decimal(participante.shopee) * Decimal(base.shopee or 0)
        + Decimal(participante.mercado_livre) * Decimal(base.ml or 0)
        + Decimal(participante.avulso) * Decimal(base.avulso or 0)
    ).quantize(Decimal("0.01"))
    coleta = db.scalar(select(Coleta).where(Coleta.participante_id == participante.id_participante))
    if not coleta:
        coleta = Coleta(
            sub_base=participante.sub_base,
            base=base.base,
            username_entregador=participante.username,
            origem="manual",
            timestamp=datetime.combine(execucao.data_operacao, time.min),
            execucao_id=execucao.id_execucao,
            participante_id=participante.id_participante,
        )
        db.add(coleta)
    coleta.shopee = participante.shopee
    coleta.mercado_livre = participante.mercado_livre
    coleta.avulso = participante.avulso
    coleta.pacotes_g = participante.pacotes_g
    coleta.g_shopee = participante.g_shopee
    coleta.g_ml = participante.g_ml
    coleta.g_avulso = participante.g_avulso
    coleta.valor_total = valor


@router.get("/config")
def obter_configuracao(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    modo = modo_coleta(db, sub_base)
    return {
        "modo_operacao": modo,
        "coleta_habilitada": modo in ("codigo", "coleta_manual", "ambos"),
        "permite_leitura": modo in ("codigo", "ambos"),
        "permite_manual": modo in ("coleta_manual", "ambos"),
    }


@router.post("/manual", response_model=ExecucaoOut, status_code=201)
def lancar_manual(
    body: ContribuicaoManualIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    exigir_modo(db, sub_base, "coleta_manual")
    _validar_data_edicao(current_user, body.data_operacao, body.origem_cliente)
    if body.client_request_id:
        existente_request = db.scalar(
            select(ColetaExecucaoParticipante).where(
                ColetaExecucaoParticipante.sub_base == sub_base,
                ColetaExecucaoParticipante.client_request_id == body.client_request_id,
            )
        )
        if existente_request:
            execucao = existente_request.execucao
            return _serializar_execucao(execucao, current_user, execucao.participantes)

    base = resolver_base(db, sub_base, base_id=body.base_id)
    executor, motoboy_id = resolver_executor(db, current_user, body.executor_user_id)
    _garantir_nao_fechado(
        db,
        sub_base=sub_base,
        base_nome=base.base,
        data_operacao=body.data_operacao,
        motoboy_id=motoboy_id,
    )
    execucao = obter_ou_criar_execucao(
        db,
        sub_base=sub_base,
        base=base,
        data_operacao=body.data_operacao,
        modo="coleta_manual",
    )
    existente = db.scalar(
        select(ColetaExecucaoParticipante).where(
            ColetaExecucaoParticipante.execucao_id == execucao.id_execucao,
            ColetaExecucaoParticipante.user_id == executor.id,
        )
    )
    if existente:
        raise HTTPException(409, "Este usuário já lançou a coleta nesta base. Use Editar.")
    participante = ColetaExecucaoParticipante(
        execucao_id=execucao.id_execucao,
        sub_base=sub_base,
        user_id=executor.id,
        motoboy_id=motoboy_id,
        username=executor.username,
        shopee=body.shopee,
        mercado_livre=body.mercado_livre,
        avulso=body.avulso,
        pacotes_g=body.pacotes_g,
        g_shopee=body.g_shopee,
        g_ml=body.g_ml,
        g_avulso=body.g_avulso,
        sem_volume=body.sem_volume,
        client_request_id=body.client_request_id,
        atualizado_por_user_id=current_user.id,
    )
    db.add(participante)
    db.flush()
    execucao.status = "sem_volume" if body.sem_volume else "coletado"
    _sincronizar_coleta_legada(db, participante, execucao)
    db.commit()
    db.refresh(execucao)
    return _serializar_execucao(execucao, current_user, execucao.participantes)


@router.patch("/participantes/{id_participante}", response_model=ExecucaoOut)
def editar_participante(
    id_participante: int,
    body: ContribuicaoManualUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    exigir_modo(db, sub_base, "coleta_manual")
    participante = db.get(ColetaExecucaoParticipante, id_participante)
    if not participante or participante.sub_base != sub_base:
        raise HTTPException(404, "Lançamento não encontrado.")
    execucao = participante.execucao
    if participante.user_id != current_user.id and not _admin(current_user):
        raise HTTPException(403, "Você só pode editar seu próprio lançamento.")
    _validar_data_edicao(current_user, execucao.data_operacao, body.origem_cliente)
    if participante.versao != body.versao:
        raise HTTPException(409, "O lançamento foi atualizado em outro dispositivo. Recarregue os dados.")
    _garantir_nao_fechado(
        db,
        sub_base=sub_base,
        base_nome=execucao.base_ref.base,
        data_operacao=execucao.data_operacao,
        motoboy_id=participante.motoboy_id,
    )
    for campo in ("shopee", "mercado_livre", "avulso", "pacotes_g", "g_shopee", "g_ml", "g_avulso"):
        setattr(participante, campo, getattr(body, campo))
    participante.sem_volume = body.sem_volume
    participante.versao += 1
    participante.atualizado_em = datetime.now()
    participante.atualizado_por_user_id = current_user.id
    execucao.status = "sem_volume" if all(p.sem_volume for p in execucao.participantes) else "coletado"
    execucao.atualizado_em = datetime.now()
    _sincronizar_coleta_legada(db, participante, execucao)
    db.commit()
    db.refresh(execucao)
    return _serializar_execucao(execucao, current_user, execucao.participantes)


@router.get("/", response_model=list[ExecucaoOut])
def listar_execucoes(
    data_inicio: date = Query(default_factory=date.today),
    data_fim: date = Query(default_factory=date.today),
    somente_minhas: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    _exigir_coleta_habilitada(db, sub_base)
    if data_fim < data_inicio or (data_fim - data_inicio).days > 366:
        raise HTTPException(422, "Período inválido ou superior a 366 dias.")
    stmt = (
        select(ColetaExecucao)
        .where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.data_operacao >= data_inicio,
            ColetaExecucao.data_operacao <= data_fim,
        )
        .order_by(ColetaExecucao.data_operacao.desc(), ColetaExecucao.base_id)
    )
    rows = db.scalars(stmt).all()
    if somente_minhas or not _admin(current_user):
        rows = [e for e in rows if any(p.user_id == current_user.id for p in e.participantes)]
    return [_serializar_execucao(e, current_user, e.participantes) for e in rows]


class ExcecaoIn(BaseModel):
    data: date
    tipo: str
    motivo: str = Field(min_length=3, max_length=500)
    base_id: Optional[int] = None


class ExcecaoOut(BaseModel):
    id_excecao: int
    data: date
    tipo: str
    motivo: str
    base_id: Optional[int]
    model_config = ConfigDict(from_attributes=True)


@router.post("/calendario", response_model=ExcecaoOut)
def salvar_excecao(
    body: ExcecaoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _admin(current_user):
        raise HTTPException(403, "Apenas admin/root pode alterar o calendário de coleta.")
    sub_base = _sub_base(current_user)
    _exigir_coleta_habilitada(db, sub_base)
    tipo = body.tipo.strip().upper()
    if tipo not in TIPOS_EXCECAO:
        raise HTTPException(422, "Tipo de exceção inválido.")
    if body.base_id is not None:
        resolver_base(db, sub_base, base_id=body.base_id)
    stmt = select(ColetaCalendarioExcecao).where(
        ColetaCalendarioExcecao.sub_base == sub_base,
        ColetaCalendarioExcecao.data == body.data,
    )
    stmt = stmt.where(
        ColetaCalendarioExcecao.base_id == body.base_id
        if body.base_id is not None
        else ColetaCalendarioExcecao.base_id.is_(None)
    )
    item = db.scalar(stmt)
    if not item:
        item = ColetaCalendarioExcecao(
            sub_base=sub_base,
            base_id=body.base_id,
            data=body.data,
            criado_por_user_id=current_user.id,
        )
        db.add(item)
    item.tipo = tipo
    item.motivo = body.motivo.strip()
    item.atualizado_em = datetime.now()
    db.commit()
    db.refresh(item)
    return item


@router.delete("/calendario/{id_excecao}", status_code=204)
def excluir_excecao(
    id_excecao: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _admin(current_user):
        raise HTTPException(403, "Apenas admin/root pode alterar o calendário de coleta.")
    _exigir_coleta_habilitada(db, _sub_base(current_user))
    item = db.get(ColetaCalendarioExcecao, id_excecao)
    if not item or item.sub_base != _sub_base(current_user):
        raise HTTPException(404, "Exceção não encontrada.")
    db.delete(item)
    db.commit()


@router.get("/calendario", response_model=list[ExcecaoOut])
def listar_excecoes(
    data_inicio: date,
    data_fim: date,
    base_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    _exigir_coleta_habilitada(db, sub_base)
    stmt = select(ColetaCalendarioExcecao).where(
        ColetaCalendarioExcecao.sub_base == sub_base,
        ColetaCalendarioExcecao.data >= data_inicio,
        ColetaCalendarioExcecao.data <= data_fim,
    )
    if base_id is not None:
        stmt = stmt.where(or_(ColetaCalendarioExcecao.base_id == base_id, ColetaCalendarioExcecao.base_id.is_(None)))
    return db.scalars(stmt.order_by(ColetaCalendarioExcecao.data)).all()


@router.get("/pendencias")
def consultar_pendencias(
    data_inicio: date,
    data_fim: date,
    base_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    _exigir_coleta_habilitada(db, sub_base)
    if data_fim < data_inicio or (data_fim - data_inicio).days > 366:
        raise HTTPException(422, "Período inválido ou superior a 366 dias.")
    bases_stmt = select(BasePreco).where(BasePreco.sub_base == sub_base, BasePreco.ativo.is_(True))
    if base_id is not None:
        bases_stmt = bases_stmt.where(BasePreco.id_base == base_id)
    bases = db.scalars(bases_stmt.order_by(BasePreco.base)).all()
    excecoes = db.scalars(
        select(ColetaCalendarioExcecao).where(
            ColetaCalendarioExcecao.sub_base == sub_base,
            ColetaCalendarioExcecao.data >= data_inicio,
            ColetaCalendarioExcecao.data <= data_fim,
        )
    ).all()
    execucoes = db.scalars(
        select(ColetaExecucao).where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.data_operacao >= data_inicio,
            ColetaExecucao.data_operacao <= data_fim,
        )
    ).all()
    coletas_legadas = db.scalars(
        select(Coleta).where(
            Coleta.sub_base == sub_base,
            Coleta.timestamp >= datetime.combine(data_inicio, time.min),
            Coleta.timestamp <= datetime.combine(data_fim, time.max),
        )
    ).all()
    ex_por_chave = {(e.base_id, e.data): e for e in excecoes}
    globais = {e.data: e for e in excecoes if e.base_id is None}
    exec_por_chave = {(e.base_id, e.data_operacao): e for e in execucoes}
    legado_por_chave = {
        ((item.base or "").strip().upper(), item.timestamp.date())
        for item in coletas_legadas
    }
    itens = []
    cursor = data_inicio
    while cursor <= data_fim:
        for base in bases:
            execucao = exec_por_chave.get((base.id_base, cursor))
            excecao = ex_por_chave.get((base.id_base, cursor)) or globais.get(cursor)
            programado = cursor.isoweekday() in set(base.dias_coleta or [])
            agenda_confirmada = bool(base.agenda_coleta_confirmada)
            esperado = agenda_confirmada and (programado or (excecao and excecao.tipo == "COLETA_EXTRA"))
            tem_legado = ((base.base or "").strip().upper(), cursor) in legado_por_chave
            if execucao:
                status_item = "COLETADO_EM_FERIADO" if excecao and excecao.tipo == "FERIADO" else execucao.status.upper()
            elif tem_legado:
                status_item = "COLETADO_LEGADO"
            elif not agenda_confirmada:
                status_item = "AGENDA_NAO_CONFIRMADA"
            elif excecao and excecao.tipo in ("FERIADO", "SEM_COLETA", "JUSTIFICADO"):
                status_item = excecao.tipo
            elif not esperado:
                status_item = "NAO_PROGRAMADO"
            else:
                status_item = "PENDENTE"
            itens.append(
                {
                    "data": cursor,
                    "base_id": base.id_base,
                    "base": base.base,
                    "status": status_item,
                    "motivo": excecao.motivo if excecao else None,
                    "id_execucao": execucao.id_execucao if execucao else None,
                }
            )
        cursor += timedelta(days=1)
    pendentes = [item for item in itens if item["status"] == "PENDENTE"]
    return {
        "pronto_para_fechamento": not pendentes,
        "total_pendentes": len(pendentes),
        "itens": itens,
    }
