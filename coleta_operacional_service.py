from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    BasePreco,
    Coleta,
    ColetaExecucao,
    ColetaExecucaoParticipante,
    Motoboy,
    Owner,
    User,
)

MODOS_COM_COLETA = {"codigo", "coleta_manual", "ambos"}


def obter_owner(db: Session, sub_base: str) -> Owner:
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(403, "Owner não encontrado para esta sub_base.")
    return owner


def modo_coleta(db: Session, sub_base: str) -> str:
    owner = obter_owner(db, sub_base)
    if bool(owner.ignorar_coleta):
        return "desativado"
    modo = (owner.modo_operacao or "codigo").strip().lower()
    return modo if modo in MODOS_COM_COLETA else "codigo"


def exigir_modo(db: Session, sub_base: str, esperado: str) -> None:
    atual = modo_coleta(db, sub_base)
    permitido = atual == esperado or atual == "ambos"
    if not permitido:
        rotulo = "manual" if esperado == "coleta_manual" else "por leitura"
        raise HTTPException(
            403,
            f"Coleta {rotulo} indisponível. O owner está configurado no modo '{atual}'.",
        )


def resolver_base(db: Session, sub_base: str, *, base_id: Optional[int] = None, nome: Optional[str] = None) -> BasePreco:
    stmt = select(BasePreco).where(BasePreco.sub_base == sub_base)
    if base_id is not None:
        stmt = stmt.where(BasePreco.id_base == base_id)
    elif nome:
        stmt = stmt.where(func.upper(BasePreco.base) == nome.strip().upper())
    else:
        raise HTTPException(422, "Informe a base da coleta.")
    base = db.scalar(stmt)
    if not base:
        raise HTTPException(404, "Base não encontrada nesta sub_base.")
    if not bool(base.ativo):
        raise HTTPException(409, "A base está inativa.")
    return base


def resolver_executor(db: Session, current_user: User, executor_user_id: Optional[int] = None) -> tuple[User, Optional[int]]:
    role_raw = getattr(current_user, "role", -1)
    role = int(role_raw) if role_raw is not None else -1
    if executor_user_id is not None and executor_user_id != current_user.id:
        if role not in (0, 1, 2):
            raise HTTPException(403, "Somente admin/root pode lançar para outro usuário.")
        executor = db.get(User, executor_user_id)
    else:
        executor = db.get(User, current_user.id)
    if not executor or executor.sub_base != current_user.sub_base or not executor.status:
        raise HTTPException(404, "Executor não encontrado nesta sub_base.")
    motoboy = db.scalar(select(Motoboy).where(Motoboy.user_id == executor.id))
    if motoboy and not bool(getattr(motoboy, "pode_realizar_coleta", False)):
        raise HTTPException(403, "Este motoboy não possui permissão para realizar coletas.")
    return executor, int(motoboy.id_motoboy) if motoboy else None


def obter_ou_criar_execucao(
    db: Session,
    *,
    sub_base: str,
    base: BasePreco,
    data_operacao: date,
    modo: str,
) -> ColetaExecucao:
    execucao = db.scalar(
        select(ColetaExecucao).where(
            ColetaExecucao.sub_base == sub_base,
            ColetaExecucao.base_id == base.id_base,
            ColetaExecucao.data_operacao == data_operacao,
        )
    )
    if execucao:
        if execucao.modo != modo:
            raise HTTPException(
                409,
                "A base já possui coleta registrada por outro modo nesta data. Edite o lançamento existente para evitar duplicidade.",
            )
        return execucao
    execucao = ColetaExecucao(
        sub_base=sub_base,
        base_id=base.id_base,
        data_operacao=data_operacao,
        modo=modo,
        status="coletado",
    )
    db.add(execucao)
    db.flush()
    return execucao


def agregar_leitura(
    db: Session,
    *,
    current_user: User,
    coleta: Coleta,
    base_nome: str,
) -> ColetaExecucaoParticipante:
    sub_base = current_user.sub_base
    exigir_modo(db, sub_base, "codigo")
    base = resolver_base(db, sub_base, nome=base_nome)
    executor, motoboy_id = resolver_executor(db, current_user)
    execucao = obter_ou_criar_execucao(
        db,
        sub_base=sub_base,
        base=base,
        data_operacao=date.today(),
        modo="codigo",
    )
    participante = db.scalar(
        select(ColetaExecucaoParticipante).where(
            ColetaExecucaoParticipante.execucao_id == execucao.id_execucao,
            ColetaExecucaoParticipante.user_id == executor.id,
        )
    )
    if not participante:
        participante = ColetaExecucaoParticipante(
            execucao_id=execucao.id_execucao,
            sub_base=sub_base,
            user_id=executor.id,
            motoboy_id=motoboy_id,
            username=executor.username,
            shopee=int(coleta.shopee or 0),
            mercado_livre=int(coleta.mercado_livre or 0),
            avulso=int(coleta.avulso or 0),
            pacotes_g=int(coleta.pacotes_g or 0),
            atualizado_por_user_id=current_user.id,
        )
        db.add(participante)
        db.flush()
    else:
        participante.shopee += int(coleta.shopee or 0)
        participante.mercado_livre += int(coleta.mercado_livre or 0)
        participante.avulso += int(coleta.avulso or 0)
        participante.pacotes_g += int(coleta.pacotes_g or 0)
    participante.sem_volume = False
    participante.versao += 1
    participante.atualizado_em = datetime.now()
    participante.atualizado_por_user_id = current_user.id
    execucao.status = "coletado"
    execucao.atualizado_em = datetime.now()
    coleta.execucao_id = execucao.id_execucao
    coleta.participante_id = participante.id_participante
    return participante
