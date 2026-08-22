from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_current_user
from coleta_operacional_service import (
    atualizar_status_execucao,
    combinar_modo_execucao,
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
# Correção financeira de quantidade: somente root/admin.
ROOT_ADMIN_ROLES = {0, 1}
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


def _root_admin(current_user: User) -> bool:
    try:
        return int(current_user.role) in ROOT_ADMIN_ROLES
    except (TypeError, ValueError):
        return False


def _valor_servicos(base: BasePreco, shopee: int, mercado_livre: int, avulso: int) -> Decimal:
    return (
        Decimal(shopee) * Decimal(base.shopee or 0)
        + Decimal(mercado_livre) * Decimal(base.ml or 0)
        + Decimal(avulso) * Decimal(base.avulso or 0)
    ).quantize(Decimal("0.01"))


def _precos_base(base: BasePreco) -> dict:
    return {
        "shopee": str(Decimal(base.shopee or 0).quantize(Decimal("0.01"))),
        "mercado_livre": str(Decimal(base.ml or 0).quantize(Decimal("0.01"))),
        "avulso": str(Decimal(base.avulso or 0).quantize(Decimal("0.01"))),
    }

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


class CorrigirQuantidadesIn(BaseModel):
    """Quantidades absolutas por serviço (admin/root). Mínimo 0."""

    shopee: int = Field(ge=0)
    mercado_livre: int = Field(ge=0)
    avulso: int = Field(ge=0)
    versao: int = Field(ge=1)
    origem_cliente: Literal["web", "mobile"] = "web"


class CorrigirQuantidadesOut(BaseModel):
    id_participante: int
    base: str
    data_operacao: date
    modo: str
    tipo_ajuste: Literal["manual", "leitura"]
    shopee: int
    mercado_livre: int
    avulso: int
    delta_shopee: int
    delta_mercado_livre: int
    delta_avulso: int
    valor_anterior: str
    valor_novo: str
    versao: int


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
    status: str
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


class IniciarColetaIn(BaseModel):
    ajudar: bool = False
    metodo: Literal["codigo", "coleta_manual"]


def _participante_atual(execucao: ColetaExecucao, user_id: int) -> Optional[ColetaExecucaoParticipante]:
    return next((item for item in execucao.participantes if item.user_id == user_id), None)


def _participantes_resumo(
    execucao: ColetaExecucao,
    current_user: User,
    base: BasePreco,
) -> list[dict]:
    is_root = _root_admin(current_user)
    status_ok = execucao.status in ("coletado", "em_coleta", "sem_volume")
    modo = execucao.modo or ""
    # Fase 1: manual/ambos. Fase 2: também leitura (codigo).
    modo_permite_correcao = modo in ("coleta_manual", "ambos", "codigo")
    itens = []
    for item in execucao.participantes:
        shopee = int(item.shopee or 0)
        mercado_livre = int(item.mercado_livre or 0)
        avulso = int(item.avulso or 0)
        valor = _valor_servicos(base, shopee, mercado_livre, avulso)
        tem_volume = shopee + mercado_livre + avulso > 0 or bool(item.sem_volume)
        pode_corrigir = bool(
            is_root
            and status_ok
            and modo_permite_correcao
            and tem_volume
        )
        itens.append(
            {
                "id_participante": item.id_participante,
                "user_id": item.user_id,
                "username": item.username,
                "status": getattr(item, "status", "finalizado"),
                "total": _quantidade_total(item),
                "shopee": shopee,
                "mercado_livre": mercado_livre,
                "avulso": avulso,
                "versao": int(item.versao or 1),
                "sem_volume": bool(item.sem_volume),
                "valor_total": str(valor),
                "pode_editar": (
                    (item.user_id == current_user.id and execucao.data_operacao == date.today())
                    or is_root
                )
                and modo in ("coleta_manual", "ambos"),
                "pode_corrigir": pode_corrigir,
            }
        )
    return itens


def _serializar_execucao(execucao: ColetaExecucao, current_user: User, participantes) -> ExecucaoOut:
    is_admin = _admin(current_user)
    is_root = _root_admin(current_user)
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
                status=getattr(p, "status", "finalizado"),
                versao=p.versao,
                total=_quantidade_total(p),
                pode_editar=(p.user_id == current_user.id and execucao.data_operacao == date.today())
                or is_root,
            )
        )
    return ExecucaoOut(
        id_execucao=execucao.id_execucao,
        base_id=execucao.base_id,
        base=execucao.base_ref.base,
        data_operacao=execucao.data_operacao,
        modo=execucao.modo,
        status=execucao.status,
        total=sum(_quantidade_total(p) for p in participantes),
        shopee=sum(int(p.shopee or 0) for p in participantes),
        mercado_livre=sum(int(p.mercado_livre or 0) for p in participantes),
        avulso=sum(int(p.avulso or 0) for p in participantes),
        participantes=itens,
    )


def _sincronizar_coleta_legada(db: Session, participante: ColetaExecucaoParticipante, execucao: ColetaExecucao) -> None:
    base = execucao.base_ref
    valor = _valor_servicos(base, participante.shopee, participante.mercado_livre, participante.avulso)
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


def _criar_coleta_ajuste(
    db: Session,
    *,
    participante: ColetaExecucaoParticipante,
    execucao: ColetaExecucao,
    delta_shopee: int,
    delta_ml: int,
    delta_avulso: int,
) -> None:
    """Ajuste financeiro na leitura: não cria/apaga Saída; só lança delta na Coleta."""
    if delta_shopee == 0 and delta_ml == 0 and delta_avulso == 0:
        return
    base = execucao.base_ref
    valor = _valor_servicos(base, delta_shopee, delta_ml, delta_avulso)
    db.add(
        Coleta(
            sub_base=participante.sub_base,
            base=base.base,
            username_entregador=participante.username,
            origem="ajuste",
            timestamp=datetime.now(),
            execucao_id=execucao.id_execucao,
            participante_id=participante.id_participante,
            shopee=delta_shopee,
            mercado_livre=delta_ml,
            avulso=delta_avulso,
            pacotes_g=0,
            g_shopee=0,
            g_ml=0,
            g_avulso=0,
            valor_total=valor,
        )
    )

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


@router.get("/situacao")
def consultar_situacao_bases(
    data_operacao: date = Query(default_factory=date.today),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    _exigir_coleta_habilitada(db, sub_base)
    resolver_executor(db, current_user)
    bases = db.scalars(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base,
            BasePreco.ativo.is_(True),
        ).order_by(BasePreco.base)
    ).all()
    execucoes = db.scalars(
        select(ColetaExecucao).where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.data_operacao == data_operacao,
        )
    ).all()
    por_base = {item.base_id: item for item in execucoes}
    is_root = _root_admin(current_user)
    itens = []
    for base in bases:
        execucao = por_base.get(base.id_base)
        status = execucao.status if execucao else "pendente"
        participantes = _participantes_resumo(execucao, current_user, base) if execucao else []
        shopee = sum(int(p.shopee or 0) for p in execucao.participantes) if execucao else 0
        mercado_livre = sum(int(p.mercado_livre or 0) for p in execucao.participantes) if execucao else 0
        avulso = sum(int(p.avulso or 0) for p in execucao.participantes) if execucao else 0
        valor_total = _valor_servicos(base, shopee, mercado_livre, avulso)
        modo = execucao.modo if execucao else None
        status_ok = status in ("coletado", "em_coleta", "sem_volume")
        pode_corrigir = bool(
            is_root
            and execucao
            and status_ok
            and modo in ("coleta_manual", "ambos", "codigo")
            and any(p.get("pode_corrigir") for p in participantes)
        )
        itens.append(
            {
                "base_id": base.id_base,
                "base": base.base,
                "endereco_completo": getattr(base, "endereco_completo", None),
                "status": status,
                "id_execucao": execucao.id_execucao if execucao else None,
                "modo": modo,
                "total": sum(item["total"] for item in participantes),
                "shopee": shopee,
                "mercado_livre": mercado_livre,
                "avulso": avulso,
                "valor_total": str(valor_total),
                "precos": _precos_base(base),
                "participantes": participantes,
                "participando": any(
                    p.user_id == current_user.id and getattr(p, "status", "finalizado") == "em_coleta"
                    for p in execucao.participantes
                ) if execucao else False,
                "pode_ajudar": bool(
                    execucao
                    and execucao.status == "em_coleta"
                    and not any(p.user_id == current_user.id for p in execucao.participantes)
                ),
                "pode_corrigir": pode_corrigir,
                "atualizado_em": execucao.atualizado_em if execucao else None,
            }
        )
    return {
        "data_operacao": data_operacao,
        "pode_corrigir_quantidades": is_root,
        "resumo": {
            "pendentes": sum(item["status"] == "pendente" for item in itens),
            "em_coleta": sum(item["status"] == "em_coleta" for item in itens),
            "coletadas": sum(item["status"] in ("coletado", "sem_volume") for item in itens),
        },
        "itens": itens,
    }

@router.post("/bases/{base_id}/iniciar", response_model=ExecucaoOut)
def iniciar_coleta(
    base_id: int,
    body: IniciarColetaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    exigir_modo(db, sub_base, body.metodo)
    base = resolver_base(db, sub_base, base_id=base_id)
    executor, motoboy_id = resolver_executor(db, current_user)
    execucao = db.scalar(
        select(ColetaExecucao).where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.base_id == base.id_base,
            ColetaExecucao.data_operacao == date.today(),
        ).with_for_update()
    )
    if not execucao:
        execucao = ColetaExecucao(
            sub_base=sub_base,
            base_id=base.id_base,
            data_operacao=date.today(),
            modo=body.metodo,
            status="em_coleta",
        )
        db.add(execucao)
        try:
            db.flush()
        except IntegrityError:
            # Duas pessoas podem tocar a mesma base quase simultaneamente.
            # A unique key base/dia decide a execução canônica; a segunda requisição passa a colaborar nela.
            db.rollback()
            execucao = db.scalar(
                select(ColetaExecucao).where(
                    ColetaExecucao.sub_base == sub_base,
                    ColetaExecucao.base_id == base_id,
                    ColetaExecucao.data_operacao == date.today(),
                ).with_for_update()
            )
            if not execucao:
                raise HTTPException(409, "A situação desta base mudou. Atualize a lista e tente novamente.")
    if execucao.status in ("coletado", "sem_volume"):
        raise HTTPException(409, "Esta base já foi coletada hoje.")
    execucao.modo = combinar_modo_execucao(execucao.modo, body.metodo)
    participante = _participante_atual(execucao, executor.id)
    outros_ativos = [p.username for p in execucao.participantes if p.user_id != executor.id and getattr(p, "status", "finalizado") == "em_coleta"]
    if not participante and outros_ativos and not body.ajudar:
        raise HTTPException(
            409,
            {
                "mensagem": f"Esta base já está em coleta por {', '.join(outros_ativos)}.",
                "participantes": outros_ativos,
                "pode_ajudar": True,
            },
        )
    if not participante:
        participante = ColetaExecucaoParticipante(
            execucao=execucao,
            sub_base=sub_base,
            user_id=executor.id,
            motoboy_id=motoboy_id,
            username=executor.username,
            status="em_coleta",
            atualizado_por_user_id=current_user.id,
        )
        db.add(participante)
    elif getattr(participante, "status", "finalizado") != "em_coleta":
        raise HTTPException(409, "Sua participação nesta coleta já foi finalizada.")
    atualizar_status_execucao(execucao)
    db.commit()
    db.refresh(execucao)
    return _serializar_execucao(execucao, current_user, execucao.participantes)


@router.post("/execucoes/{id_execucao}/finalizar", response_model=ExecucaoOut)
def finalizar_coleta(
    id_execucao: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    execucao = db.scalar(
        select(ColetaExecucao).where(
            ColetaExecucao.id_execucao == id_execucao,
            ColetaExecucao.sub_base == sub_base,
        ).with_for_update()
    )
    if not execucao:
        raise HTTPException(404, "Coleta não encontrada.")
    participante = _participante_atual(execucao, current_user.id)
    if not participante and not _admin(current_user):
        raise HTTPException(403, "Você não participa desta coleta.")
    if participante:
        if _quantidade_total(participante) == 0 and not participante.sem_volume:
            raise HTTPException(422, "Registre ao menos um pacote/quantidade ou informe coleta sem volume.")
        participante.status = "finalizado"
        participante.versao += 1
        participante.atualizado_em = datetime.now()
        participante.atualizado_por_user_id = current_user.id
    elif _admin(current_user):
        for item in execucao.participantes:
            if _quantidade_total(item) == 0 and not item.sem_volume:
                raise HTTPException(422, f"{item.username} ainda não informou volume.")
            item.status = "finalizado"
    atualizar_status_execucao(execucao)
    db.commit()
    db.refresh(execucao)
    return _serializar_execucao(execucao, current_user, execucao.participantes)


@router.delete("/execucoes/{id_execucao}/participacao", status_code=204)
def liberar_participacao(
    id_execucao: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _sub_base(current_user)
    execucao = db.scalar(
        select(ColetaExecucao).where(
            ColetaExecucao.id_execucao == id_execucao,
            ColetaExecucao.sub_base == sub_base,
        ).with_for_update()
    )
    if not execucao:
        raise HTTPException(404, "Coleta não encontrada.")
    participante = _participante_atual(execucao, current_user.id)
    if not participante:
        raise HTTPException(404, "Participação não encontrada.")
    if _quantidade_total(participante) or participante.sem_volume:
        raise HTTPException(409, "Não é possível sair após registrar volumes. Finalize a coleta.")
    db.delete(participante)
    db.flush()
    restantes = [item for item in execucao.participantes if item.id_participante != participante.id_participante]
    if not restantes:
        db.delete(execucao)
    else:
        if any(getattr(item, "status", "finalizado") == "em_coleta" for item in restantes):
            execucao.status = "em_coleta"
        elif all(bool(item.sem_volume) for item in restantes):
            execucao.status = "sem_volume"
        else:
            execucao.status = "coletado"
        execucao.atualizado_em = datetime.now()
    db.commit()


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
    if existente and getattr(existente, "status", "finalizado") != "em_coleta":
        raise HTTPException(409, "Este usuário já lançou a coleta nesta base. Use Editar.")
    participante = existente or ColetaExecucaoParticipante(
        execucao_id=execucao.id_execucao,
        sub_base=sub_base,
        user_id=executor.id,
        motoboy_id=motoboy_id,
        username=executor.username,
        atualizado_por_user_id=current_user.id,
    )
    participante.shopee = body.shopee
    participante.mercado_livre = body.mercado_livre
    participante.avulso = body.avulso
    participante.pacotes_g = body.pacotes_g
    participante.g_shopee = body.g_shopee
    participante.g_ml = body.g_ml
    participante.g_avulso = body.g_avulso
    participante.sem_volume = body.sem_volume
    participante.client_request_id = body.client_request_id
    participante.status = "finalizado"
    participante.atualizado_em = datetime.now()
    participante.atualizado_por_user_id = current_user.id
    if not existente:
        db.add(participante)
    db.flush()
    atualizar_status_execucao(execucao)
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
    # Edição de lançamento próprio: usuário do dia. Edição de outro: só root/admin.
    if participante.user_id != current_user.id and not _root_admin(current_user):
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
    participante.status = "finalizado"
    atualizar_status_execucao(execucao)
    _sincronizar_coleta_legada(db, participante, execucao)
    db.commit()
    db.refresh(execucao)
    return _serializar_execucao(execucao, current_user, execucao.participantes)


@router.post("/participantes/{id_participante}/corrigir", response_model=CorrigirQuantidadesOut)
def corrigir_quantidades_participante(
    id_participante: int,
    body: CorrigirQuantidadesIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Correção de quantidade absoluta por serviço (Flex/Shopee/Avulso).
    - Manual: sincroniza Coleta ligada ao participante.
    - Leitura: lança Coleta origem=ajuste com o delta (sem criar/apagar Saída).
    Somente root/admin (roles 0 e 1).
    """
    if not _root_admin(current_user):
        raise HTTPException(403, "Somente administrador ou root pode corrigir quantidades.")
    sub_base = _sub_base(current_user)
    _exigir_coleta_habilitada(db, sub_base)
    participante = db.get(ColetaExecucaoParticipante, id_participante)
    if not participante or participante.sub_base != sub_base:
        raise HTTPException(404, "Lançamento não encontrado.")
    execucao = participante.execucao
    if not execucao:
        raise HTTPException(404, "Execução não encontrada.")
    if execucao.status not in ("coletado", "em_coleta", "sem_volume"):
        raise HTTPException(409, "Só é possível corrigir bases em coleta ou já coletadas.")
    if execucao.modo not in ("coleta_manual", "ambos", "codigo"):
        raise HTTPException(409, "Modo de coleta não permite correção.")
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

    ant_shopee = int(participante.shopee or 0)
    ant_ml = int(participante.mercado_livre or 0)
    ant_avulso = int(participante.avulso or 0)
    if ant_shopee + ant_ml + ant_avulso == 0 and not participante.sem_volume:
        raise HTTPException(409, "Não há quantidade para corrigir. Registre a coleta primeiro.")

    delta_shopee = body.shopee - ant_shopee
    delta_ml = body.mercado_livre - ant_ml
    delta_avulso = body.avulso - ant_avulso
    if delta_shopee == 0 and delta_ml == 0 and delta_avulso == 0:
        raise HTTPException(422, "Nenhuma quantidade foi alterada.")

    base = execucao.base_ref
    valor_anterior = _valor_servicos(base, ant_shopee, ant_ml, ant_avulso)
    valor_novo = _valor_servicos(base, body.shopee, body.mercado_livre, body.avulso)

    # Leitura pura ou ambos com origem de códigos: ajuste financeiro separado.
    # Manual puro: sincroniza a Coleta legado do participante.
    coletas_ligadas = db.scalars(
        select(Coleta).where(Coleta.participante_id == participante.id_participante)
    ).all()
    tem_leitura = any((c.origem or "") in ("codigo", "leitura", "lote") for c in coletas_ligadas)
    # Manual puro: sincroniza a Coleta do participante.
    # Leitura (ou mistura com leitura): lança ajuste financeiro sem criar/apagar Saída.
    if execucao.modo == "coleta_manual":
        usar_ajuste_leitura = False
    elif execucao.modo == "codigo":
        usar_ajuste_leitura = True
    else:
        # ambos: se houver leitura vinculada, não sobrescreve lotes
        usar_ajuste_leitura = tem_leitura

    participante.shopee = body.shopee
    participante.mercado_livre = body.mercado_livre
    participante.avulso = body.avulso
    participante.sem_volume = body.shopee + body.mercado_livre + body.avulso == 0
    participante.versao += 1
    participante.atualizado_em = datetime.now()
    participante.atualizado_por_user_id = current_user.id
    participante.status = "finalizado"
    atualizar_status_execucao(execucao)

    if usar_ajuste_leitura:
        _criar_coleta_ajuste(
            db,
            participante=participante,
            execucao=execucao,
            delta_shopee=delta_shopee,
            delta_ml=delta_ml,
            delta_avulso=delta_avulso,
        )
        tipo_ajuste: Literal["manual", "leitura"] = "leitura"
    else:
        _sincronizar_coleta_legada(db, participante, execucao)
        tipo_ajuste = "manual"

    db.commit()
    return CorrigirQuantidadesOut(
        id_participante=participante.id_participante,
        base=base.base,
        data_operacao=execucao.data_operacao,
        modo=execucao.modo,
        tipo_ajuste=tipo_ajuste,
        shopee=participante.shopee,
        mercado_livre=participante.mercado_livre,
        avulso=participante.avulso,
        delta_shopee=delta_shopee,
        delta_mercado_livre=delta_ml,
        delta_avulso=delta_avulso,
        valor_anterior=str(valor_anterior),
        valor_novo=str(valor_novo),
        versao=participante.versao,
    )

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
    pendentes = [item for item in itens if item["status"] in ("PENDENTE", "EM_COLETA")]
    return {
        "pronto_para_fechamento": not pendentes,
        "total_pendentes": len(pendentes),
        "itens": itens,
    }
