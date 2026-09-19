"""Políticas gerais da base (operação + padrões Motoboy)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import _coerce_role_int, bump_motoboys_claims_version_for_sub_base, get_current_user
from db import get_db
from models import Motoboy, Owner, User

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
    pode_realizar_coleta: bool = False
    pode_ler_saida: bool = True
    pode_digitar_codigo_manual: bool = False
    pode_lancar_avulso: bool = True
    avulso_exige_foto: bool = True


class PoliticasOut(BaseModel):
    operacao: OperacaoPoliticas
    padroes_motoboy: PadroesMotoboyPoliticas


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
    avulso_exige_foto: Optional[bool] = None


class PoliticasPatch(BaseModel):
    operacao: Optional[OperacaoPoliticasPatch] = None
    padroes_motoboy: Optional[PadroesMotoboyPatch] = None
    aplicar_padroes_aos_motoboys: bool = False


def _assert_admin(current_user: User) -> None:
    role = _coerce_role_int(getattr(current_user, "role", None))
    if role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a administradores.")


def _owner_for_user(db: Session, current_user: User) -> Owner:
    sub_base = (getattr(current_user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida.")
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")
    return owner


def _owner_to_out(owner: Owner) -> PoliticasOut:
    return PoliticasOut(
        operacao=OperacaoPoliticas(
            coleta_habilitada=not bool(owner.ignorar_coleta),
            modo_operacao=(owner.modo_operacao or "codigo").strip().lower() or "codigo",
            bloquear_saida_sem_coleta=bool(getattr(owner, "bloquear_saida_sem_coleta", False)),
            entrada_habilitada=bool(getattr(owner, "entrada_obrigatoria_habilitada", False)),
            conferencia_saida_habilitada=bool(getattr(owner, "conferencia_saida_habilitada", False)),
            devolucao_sub_base_habilitada=bool(getattr(owner, "devolucao_sub_base_habilitada", False)),
        ),
        padroes_motoboy=PadroesMotoboyPoliticas(
            pode_realizar_coleta=bool(getattr(owner, "default_pode_realizar_coleta", False)),
            pode_ler_saida=bool(getattr(owner, "default_pode_ler_saida", True)),
            pode_digitar_codigo_manual=bool(getattr(owner, "default_pode_digitar_codigo_manual", False)),
            pode_lancar_avulso=bool(getattr(owner, "default_pode_lancar_avulso", True)),
            avulso_exige_foto=bool(getattr(owner, "default_avulso_exige_foto", True)),
        ),
    )


@router.get("", response_model=PoliticasOut)
@router.get("/", response_model=PoliticasOut)
def get_politicas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _assert_admin(current_user)
    return _owner_to_out(_owner_for_user(db, current_user))


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
        if p.pode_lancar_avulso is not None:
            owner.default_pode_lancar_avulso = bool(p.pode_lancar_avulso)
        if p.avulso_exige_foto is not None:
            owner.default_avulso_exige_foto = bool(p.avulso_exige_foto)
        if not owner.default_pode_lancar_avulso:
            owner.default_avulso_exige_foto = False

    aplicados = 0
    if body.aplicar_padroes_aos_motoboys:
        motoboys = list(db.scalars(select(Motoboy).where(Motoboy.sub_base == sub_base)).all())
        for m in motoboys:
            m.pode_realizar_coleta = bool(owner.default_pode_realizar_coleta)
            m.pode_ler_coleta = bool(owner.default_pode_realizar_coleta)
            m.pode_ler_saida = bool(owner.default_pode_ler_saida)
            m.pode_digitar_codigo_manual = bool(owner.default_pode_digitar_codigo_manual)
            m.pode_lancar_avulso = bool(owner.default_pode_lancar_avulso)
            m.avulso_exige_foto = bool(owner.default_avulso_exige_foto) and bool(m.pode_lancar_avulso)
            m.claims_version = int(getattr(m, "claims_version", 0) or 0) + 1
            db.add(m)
            aplicados += 1
    elif operacao_changed:
        # Owner flags no JWT: força refresh silencioso dos motoboys da base
        bump_motoboys_claims_version_for_sub_base(db, sub_base)

    if owner.ignorar_coleta:
        # Coleta off: permissão de coleta nos defaults não se aplica na prática
        pass

    db.add(owner)
    db.commit()
    db.refresh(owner)
    out = _owner_to_out(owner)
    return out
