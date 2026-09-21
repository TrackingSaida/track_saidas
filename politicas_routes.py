"""Políticas gerais da base (operação + padrões Motoboy)."""
from __future__ import annotations

from cobertura_cep_service import list_prefixos_ativos, listar_cobertura_estruturada, replace_prefixos, replace_regioes
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import _coerce_role_int, bump_motoboys_claims_version_for_sub_base, get_current_user
from base import _resolve_user_sub_base
from db import get_db
from leitura_manual_auth import (
    apply_motoboy_avulso_padroes,
    apply_owner_avulso_padroes,
    flush_motoboy_avulso_columns,
    flush_owner_avulso_columns,
    list_motoboys_da_sub_base,
    list_users_role4_da_sub_base,
    resolve_owner_avulso_defaults,
)
from models import Owner, User

router = APIRouter(prefix="/politicas", tags=["Políticas gerais"])

MODOS_OPERACAO = {"codigo", "coleta_manual", "ambos"}


class OperacaoPoliticas(BaseModel):
    coleta_habilitada: bool = True
    modo_operacao: str = "codigo"
    bloquear_saida_sem_coleta: bool = False
    entrada_habilitada: bool = False
    conferencia_saida_habilitada: bool = False
    devolucao_sub_base_habilitada: bool = False


class PadroesMotoboyPoliticas(BaseModel):
    """Preset oficial: ler saídas + avulso coleta + foto; demais off."""

    pode_realizar_coleta: bool = False
    pode_ler_saida: bool = True
    pode_digitar_codigo_manual: bool = False
    pode_lancar_avulso: bool = True
    pode_criar_avulso_coleta: bool = True
    pode_criar_avulso_saida: bool = False
    avulso_exige_foto: bool = True


class CoberturaRegiaoIn(BaseModel):
    nome: str
    prefixos: List[str] = Field(default_factory=list)


class CoberturaPoliticas(BaseModel):
    prefixos: List[str] = Field(default_factory=list)
    regioes: List[Dict[str, Any]] = Field(default_factory=list)
    modo: str = "ilimitado"
    prefixos_sem_regiao: List[str] = Field(default_factory=list)
    limite_diario_default: int = 50
    expiracao_dias: int = 30


class PoliticasOut(BaseModel):
    operacao: OperacaoPoliticas
    padroes_motoboy: PadroesMotoboyPoliticas
    cobertura: CoberturaPoliticas
    motoboys_atualizados: Optional[int] = None
    motoboys_sem_perfil: Optional[int] = None


class OperacaoPoliticasPatch(BaseModel):
    coleta_habilitada: Optional[bool] = None
    modo_operacao: Optional[str] = None
    bloquear_saida_sem_coleta: Optional[bool] = None
    entrada_habilitada: Optional[bool] = None
    conferencia_saida_habilitada: Optional[bool] = None
    devolucao_sub_base_habilitada: Optional[bool] = None


class PadroesMotoboyPatch(BaseModel):
    pode_realizar_coleta: Optional[bool] = None
    pode_ler_saida: Optional[bool] = None
    pode_digitar_codigo_manual: Optional[bool] = None
    pode_lancar_avulso: Optional[bool] = None
    pode_criar_avulso_coleta: Optional[bool] = None
    pode_criar_avulso_saida: Optional[bool] = None
    avulso_exige_foto: Optional[bool] = None


class CoberturaPoliticasPatch(BaseModel):
    prefixos: Optional[List[str]] = None
    regioes: Optional[List[CoberturaRegiaoIn]] = None
    limite_diario_default: Optional[int] = Field(default=None, ge=1, le=9999)
    expiracao_dias: Optional[int] = Field(default=None, ge=1, le=365)


class PoliticasPatch(BaseModel):
    operacao: Optional[OperacaoPoliticasPatch] = None
    padroes_motoboy: Optional[PadroesMotoboyPatch] = None
    cobertura: Optional[CoberturaPoliticasPatch] = None
    aplicar_padroes_aos_motoboys: bool = False


def _assert_admin(current_user: User) -> None:
    role = _coerce_role_int(getattr(current_user, "role", None))
    if role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a administradores.")


def _owner_for_user(db: Session, current_user: User) -> Owner:
    sub_base = (_resolve_user_sub_base(db, current_user) or "").strip()
    if not sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida.")
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")
    return owner


def _owner_to_out(
    owner: Owner,
    db: Optional[Session] = None,
    *,
    motoboys_atualizados: Optional[int] = None,
    motoboys_sem_perfil: Optional[int] = None,
    padroes_override: Optional[PadroesMotoboyPoliticas] = None,
) -> PoliticasOut:
    sub = (getattr(owner, "sub_base", None) or "").strip()
    if db is not None:
        estrutura = listar_cobertura_estruturada(db, sub)
        prefixos = list_prefixos_ativos(db, sub)
    else:
        estrutura = {"regioes": [], "modo": "ilimitado", "prefixos_sem_regiao": []}
        prefixos = []
    if padroes_override is not None:
        padroes = padroes_override
    else:
        avulso_coleta, avulso_saida = resolve_owner_avulso_defaults(owner)
        padroes = PadroesMotoboyPoliticas(
            pode_realizar_coleta=bool(getattr(owner, "default_pode_realizar_coleta", False)),
            pode_ler_saida=bool(getattr(owner, "default_pode_ler_saida", True)),
            pode_digitar_codigo_manual=bool(getattr(owner, "default_pode_digitar_codigo_manual", False)),
            pode_lancar_avulso=bool(avulso_coleta or avulso_saida),
            pode_criar_avulso_coleta=avulso_coleta,
            pode_criar_avulso_saida=avulso_saida,
            avulso_exige_foto=bool(getattr(owner, "default_avulso_exige_foto", True)),
        )
    return PoliticasOut(
        operacao=OperacaoPoliticas(
            coleta_habilitada=not bool(owner.ignorar_coleta),
            modo_operacao=(owner.modo_operacao or "codigo").strip().lower() or "codigo",
            bloquear_saida_sem_coleta=bool(getattr(owner, "bloquear_saida_sem_coleta", False)),
            entrada_habilitada=bool(getattr(owner, "entrada_obrigatoria_habilitada", False)),
            conferencia_saida_habilitada=bool(getattr(owner, "conferencia_saida_habilitada", False)),
            devolucao_sub_base_habilitada=bool(getattr(owner, "devolucao_sub_base_habilitada", False)),
        ),
        padroes_motoboy=padroes,
        cobertura=CoberturaPoliticas(
            prefixos=prefixos,
            regioes=list(estrutura.get("regioes") or []),
            modo=str(estrutura.get("modo") or "ilimitado"),
            prefixos_sem_regiao=list(estrutura.get("prefixos_sem_regiao") or []),
            limite_diario_default=int(getattr(owner, "etiqueta_limite_diario_default", None) or 50),
            expiracao_dias=int(getattr(owner, "etiqueta_expiracao_dias", None) or 30),
        ),
        motoboys_atualizados=motoboys_atualizados,
        motoboys_sem_perfil=motoboys_sem_perfil,
    )


def _padroes_from_owner(owner: Owner) -> PadroesMotoboyPoliticas:
    """Snapshot em memória dos padrões (imune a expire_on_commit após db.commit)."""
    avulso_coleta, avulso_saida = resolve_owner_avulso_defaults(owner)
    return PadroesMotoboyPoliticas(
        pode_realizar_coleta=bool(getattr(owner, "default_pode_realizar_coleta", False)),
        pode_ler_saida=bool(getattr(owner, "default_pode_ler_saida", True)),
        pode_digitar_codigo_manual=bool(getattr(owner, "default_pode_digitar_codigo_manual", False)),
        pode_lancar_avulso=bool(avulso_coleta or avulso_saida),
        pode_criar_avulso_coleta=avulso_coleta,
        pode_criar_avulso_saida=avulso_saida,
        avulso_exige_foto=bool(getattr(owner, "default_avulso_exige_foto", True)),
    )


def _restore_owner_padroes(owner: Owner, pad: PadroesMotoboyPoliticas) -> None:
    """Reaplica snapshot no objeto após commit (expire_on_commit) sem SELECT stale."""
    owner.default_pode_realizar_coleta = bool(pad.pode_realizar_coleta)
    owner.default_pode_ler_saida = bool(pad.pode_ler_saida)
    owner.default_pode_digitar_codigo_manual = bool(pad.pode_digitar_codigo_manual)
    owner.default_pode_criar_avulso_coleta = bool(pad.pode_criar_avulso_coleta)
    owner.default_pode_criar_avulso_saida = bool(pad.pode_criar_avulso_saida)
    owner.default_pode_lancar_avulso = bool(pad.pode_lancar_avulso)
    owner.default_avulso_exige_foto = bool(pad.avulso_exige_foto)


@router.get("", response_model=PoliticasOut)
@router.get("/", response_model=PoliticasOut)
def get_politicas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    owner = _owner_for_user(db, current_user)
    return _owner_to_out(owner, db)


@router.post("", response_model=PoliticasOut)
@router.post("/", response_model=PoliticasOut)
@router.put("", response_model=PoliticasOut)
@router.put("/", response_model=PoliticasOut)
@router.patch("", response_model=PoliticasOut)
@router.patch("/", response_model=PoliticasOut)
def patch_politicas(
    body: PoliticasPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    owner = _owner_for_user(db, current_user)
    sub_base = (owner.sub_base or "").strip()
    operacao_changed = False

    if body.operacao:
        op = body.operacao
        if op.coleta_habilitada is not None:
            owner.ignorar_coleta = not bool(op.coleta_habilitada)
            operacao_changed = True
        if op.modo_operacao is not None:
            modo = op.modo_operacao.strip().lower()
            if modo not in MODOS_OPERACAO:
                raise HTTPException(422, "modo_operacao deve ser 'codigo', 'coleta_manual' ou 'ambos'.")
            # Só aplica se coleta habilitada (ou se está ligando junto)
            coleta_on = not bool(owner.ignorar_coleta)
            if op.coleta_habilitada is not None:
                coleta_on = bool(op.coleta_habilitada)
            if coleta_on:
                owner.modo_operacao = modo
                operacao_changed = True
        if op.bloquear_saida_sem_coleta is not None:
            owner.bloquear_saida_sem_coleta = bool(op.bloquear_saida_sem_coleta)
            operacao_changed = True
        if op.entrada_habilitada is not None:
            owner.entrada_obrigatoria_habilitada = bool(op.entrada_habilitada)
            operacao_changed = True
        if op.conferencia_saida_habilitada is not None:
            owner.conferencia_saida_habilitada = bool(op.conferencia_saida_habilitada)
            operacao_changed = True
        if op.devolucao_sub_base_habilitada is not None:
            owner.devolucao_sub_base_habilitada = bool(op.devolucao_sub_base_habilitada)
            operacao_changed = True

    if body.padroes_motoboy:
        p = body.padroes_motoboy
        if p.pode_realizar_coleta is not None:
            owner.default_pode_realizar_coleta = bool(p.pode_realizar_coleta)
        if p.pode_ler_saida is not None:
            owner.default_pode_ler_saida = bool(p.pode_ler_saida)
        if p.pode_digitar_codigo_manual is not None:
            owner.default_pode_digitar_codigo_manual = bool(p.pode_digitar_codigo_manual)
        avulso_flags = p.model_dump(exclude_unset=True)
        apply_owner_avulso_padroes(
            owner,
            pode_criar_avulso_coleta=avulso_flags.get("pode_criar_avulso_coleta"),
            pode_criar_avulso_saida=avulso_flags.get("pode_criar_avulso_saida"),
            pode_lancar_avulso=avulso_flags.get("pode_lancar_avulso"),
        )
        if p.avulso_exige_foto is not None:
            owner.default_avulso_exige_foto = bool(p.avulso_exige_foto)
        if not bool(owner.default_pode_lancar_avulso):
            owner.default_avulso_exige_foto = False
        flush_owner_avulso_columns(db, owner)

    if body.cobertura:
        cob = body.cobertura
        if cob.regioes is not None:
            replace_regioes(
                db,
                sub_base,
                [{"nome": r.nome, "prefixos": list(r.prefixos or [])} for r in cob.regioes],
            )
        elif cob.prefixos is not None:
            replace_prefixos(db, sub_base, cob.prefixos)
        if cob.limite_diario_default is not None:
            owner.etiqueta_limite_diario_default = int(cob.limite_diario_default)
        if cob.expiracao_dias is not None:
            owner.etiqueta_expiracao_dias = int(cob.expiracao_dias)

    aplicados = 0
    sem_perfil = 0
    if body.aplicar_padroes_aos_motoboys:
        avulso_coleta, avulso_saida = resolve_owner_avulso_defaults(owner)
        users_role4 = list_users_role4_da_sub_base(db, sub_base)
        sem_perfil = sum(1 for u in users_role4 if getattr(u, "motoboy", None) is None)
        motoboys = list_motoboys_da_sub_base(db, sub_base)
        for m in motoboys:
            m.pode_realizar_coleta = bool(owner.default_pode_realizar_coleta)
            m.pode_ler_coleta = bool(owner.default_pode_realizar_coleta)
            m.pode_ler_saida = bool(owner.default_pode_ler_saida)
            m.pode_digitar_codigo_manual = bool(owner.default_pode_digitar_codigo_manual)
            apply_motoboy_avulso_padroes(
                m,
                pode_criar_avulso_coleta=avulso_coleta,
                pode_criar_avulso_saida=avulso_saida,
            )
            m.avulso_exige_foto = bool(owner.default_avulso_exige_foto) and bool(m.pode_lancar_avulso)
            m.claims_version = int(getattr(m, "claims_version", 0) or 0) + 1
            db.add(m)
            flush_motoboy_avulso_columns(db, m)
            aplicados += 1
    elif operacao_changed:
        bump_motoboys_claims_version_for_sub_base(db, sub_base)

    if owner.ignorar_coleta:
        pass

    # Garante UPDATE Core das colunas de avulso como último write antes do commit.
    if body.padroes_motoboy:
        flush_owner_avulso_columns(db, owner)

    # Snapshot ANTES do commit: expire_on_commit recarrega o Owner e pode
    # devolver valores stale (coleta/foto false) se o UPDATE Core/ORM divergir.
    padroes_snap = _padroes_from_owner(owner)

    db.add(owner)
    db.commit()

    _restore_owner_padroes(owner, padroes_snap)
    out = _owner_to_out(
        owner,
        db,
        motoboys_atualizados=aplicados if body.aplicar_padroes_aos_motoboys else None,
        motoboys_sem_perfil=sem_perfil if body.aplicar_padroes_aos_motoboys else None,
        padroes_override=padroes_snap,
    )
    return out
