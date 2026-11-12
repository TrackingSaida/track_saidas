"""Serviço transacional do ciclo de confirmação real de saída (PRD-012)."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    ConferenciaSaida,
    Motoboy,
    Owner,
    Saida,
    SaidaHistorico,
    SaidaSessao,
    SaidaSessaoItem,
)

STATUS_AGUARDANDO_CONFIRMACAO = "AGUARDANDO_CONFIRMACAO_SAIDA"
STATUS_SAIU_PARA_ENTREGA = "SAIU_PARA_ENTREGA"
ESTADO_ABERTA = "ABERTA"
ESTADO_FINALIZADA = "FINALIZADA"
ESTADO_CANCELADA = "CANCELADA"


def fluxo_confirmacao_ativo(db: Session, sub_base: str) -> bool:
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return bool(owner and getattr(owner, "confirmacao_saida_sessoes_habilitada", False))


def _owner_id(db: Session, sub_base: str) -> Optional[int]:
    owner = db.scalar(select(Owner.id_owner).where(Owner.sub_base == sub_base))
    return int(owner) if owner is not None else None


def _evento(
    db: Session,
    saida: Saida,
    evento: str,
    *,
    user_id: Optional[int],
    status_anterior: Optional[str] = None,
    status_novo: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento=evento,
            status_anterior=status_anterior,
            status_novo=status_novo,
            motoboy_id_anterior=saida.motoboy_id,
            motoboy_id_novo=saida.motoboy_id,
            user_id=user_id,
            payload=json.dumps(payload or {}, ensure_ascii=False),
        )
    )


def obter_sessao_aberta(
    db: Session, *, sub_base: str, motoboy_id: int, bloquear: bool = False
) -> Optional[SaidaSessao]:
    stmt = select(SaidaSessao).where(
        SaidaSessao.sub_base == sub_base,
        SaidaSessao.motoboy_id == motoboy_id,
        SaidaSessao.estado == ESTADO_ABERTA,
    )
    if bloquear:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def obter_ou_criar_sessao_aberta(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    data_ref: date,
    user_id: Optional[int],
) -> SaidaSessao:
    # Serializa a criação por motoboy antes de consultar a sessão aberta.
    # O lock apenas na tabela de sessões não protege o caso em que ainda não
    # existe linha e duas leituras concorrentes tentam criar a primeira sessão.
    motoboy = db.scalar(
        select(Motoboy)
        .where(Motoboy.id_motoboy == motoboy_id)
        .with_for_update()
    )
    if motoboy is None:
        raise HTTPException(404, detail={"code": "MOTOBOY_NAO_ENCONTRADO"})
    sessao = obter_sessao_aberta(
        db, sub_base=sub_base, motoboy_id=motoboy_id, bloquear=True
    )
    if sessao is not None:
        return sessao
    sessao = SaidaSessao(
        owner_id=_owner_id(db, sub_base),
        sub_base=sub_base,
        motoboy_id=motoboy_id,
        data_ref=data_ref,
        estado=ESTADO_ABERTA,
        aberta_por_user_id=user_id,
    )
    db.add(sessao)
    db.flush()
    return sessao


def invalidar_vinculo_operacional(
    db: Session,
    *,
    id_saida: int,
    motivo: str,
    user_id: Optional[int],
) -> int:
    itens = db.scalars(
        select(SaidaSessaoItem)
        .where(
            SaidaSessaoItem.id_saida == id_saida,
            SaidaSessaoItem.ativo_operacional.is_(True),
        )
        .with_for_update()
    ).all()
    now = datetime.utcnow()
    for item in itens:
        item.ativo_operacional = False
        item.removido_em = now
        item.removido_por_user_id = user_id
        item.motivo_remocao = motivo
        item.sessao.versao = int(item.sessao.versao or 0) + 1
        item.sessao.atualizada_em = now
    return len(itens)


def incluir_saida_na_sessao(
    db: Session,
    *,
    saida: Saida,
    sub_base: str,
    motoboy_id: int,
    user_id: Optional[int],
    flag_ja_validada: bool = False,
) -> SaidaSessao:
    if not flag_ja_validada and not fluxo_confirmacao_ativo(db, sub_base):
        raise RuntimeError("Fluxo de confirmação por sessão está desabilitado.")
    existente = db.scalar(
        select(SaidaSessaoItem)
        .where(
            SaidaSessaoItem.id_saida == saida.id_saida,
            SaidaSessaoItem.ativo_operacional.is_(True),
        )
        .with_for_update()
    )
    if existente is not None:
        sessao_existente = db.get(SaidaSessao, existente.sessao_id)
        if (
            sessao_existente is not None
            and sessao_existente.estado == ESTADO_ABERTA
            and sessao_existente.sub_base == sub_base
            and int(sessao_existente.motoboy_id) == int(motoboy_id)
        ):
            return sessao_existente
        invalidar_vinculo_operacional(
            db, id_saida=int(saida.id_saida), motivo="NOVO_CICLO_OPERACIONAL", user_id=user_id
        )

    sessao = obter_ou_criar_sessao_aberta(
        db,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
        data_ref=date.today(),
        user_id=user_id,
    )
    db.add(
        SaidaSessaoItem(
            sessao_id=sessao.id,
            id_saida=saida.id_saida,
            codigo_snapshot=saida.codigo,
            servico_snapshot=saida.servico,
            status_snapshot=saida.status,
            incluido_por_user_id=user_id,
        )
    )
    sessao.versao = int(sessao.versao or 0) + 1
    sessao.atualizada_em = datetime.utcnow()
    db.flush()
    return sessao


def _itens_operacionais(db: Session, sessao: SaidaSessao) -> list[tuple[SaidaSessaoItem, Saida]]:
    rows = db.execute(
        select(SaidaSessaoItem, Saida)
        .join(Saida, Saida.id_saida == SaidaSessaoItem.id_saida)
        .where(
            SaidaSessaoItem.sessao_id == sessao.id,
            SaidaSessaoItem.ativo_operacional.is_(True),
            Saida.sub_base == sessao.sub_base,
            Saida.motoboy_id == sessao.motoboy_id,
        )
        .order_by(SaidaSessaoItem.id)
    ).all()
    return list(rows)


def _bucket(servico: Optional[str]) -> str:
    valor = (servico or "").strip().lower()
    if valor == "shopee":
        return "shopee"
    if valor in {"ml", "mercado_livre", "mercado livre", "mercadolivre"}:
        return "ml"
    return "avulso"


def finalizar_sessao(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    user_id: Optional[int],
    sessao_id: Optional[int] = None,
) -> SaidaSessao:
    filtro_estado_ou_id = (
        SaidaSessao.id == sessao_id
        if sessao_id is not None
        else SaidaSessao.estado == ESTADO_ABERTA
    )
    stmt = select(SaidaSessao).where(
        SaidaSessao.sub_base == sub_base,
        SaidaSessao.motoboy_id == motoboy_id,
        filtro_estado_ou_id,
    ).with_for_update()
    sessao = db.scalar(stmt)
    if sessao is None:
        raise HTTPException(404, detail={"code": "SESSAO_NAO_ENCONTRADA"})
    if sessao.estado == ESTADO_FINALIZADA:
        return sessao
    if sessao.estado != ESTADO_ABERTA:
        raise HTTPException(409, detail={"code": "SESSAO_NAO_ABERTA"})

    itens = _itens_operacionais(db, sessao)
    now = datetime.utcnow()
    if not itens:
        sessao.estado = ESTADO_CANCELADA
        sessao.cancelada_em = now
        sessao.cancelada_por_user_id = user_id
        sessao.motivo_cancelamento = "SESSAO_VAZIA"
        sessao.atualizada_em = now
        db.flush()
        return sessao

    totais = {"shopee": 0, "ml": 0, "avulso": 0}
    for _, saida in itens:
        totais[_bucket(saida.servico)] += 1
        _evento(
            db,
            saida,
            "sessao_leitura_finalizada",
            user_id=user_id,
            status_anterior=saida.status,
            status_novo=saida.status,
            payload={"sessao_id": int(sessao.id)},
        )
    sessao.estado = ESTADO_FINALIZADA
    sessao.finalizada_em = now
    sessao.finalizada_por_user_id = user_id
    sessao.atualizada_em = now
    sessao.total_snapshot = len(itens)
    sessao.total_shopee_snapshot = totais["shopee"]
    sessao.total_ml_snapshot = totais["ml"]
    sessao.total_avulso_snapshot = totais["avulso"]
    conferencia = db.scalar(
        select(ConferenciaSaida).where(ConferenciaSaida.sessao_id == sessao.id)
    )
    if conferencia is None:
        db.add(
            ConferenciaSaida(
                sessao_id=sessao.id,
                sub_base=sub_base,
                owner_id=sessao.owner_id,
                motoboy_id=motoboy_id,
                data_ref=sessao.data_ref,
                status="pendente",
                ultima_abertura_em=now,
                qtd_no_momento=len(itens),
            )
        )
    db.flush()
    return sessao


def sessoes_pendentes_motoboy(
    db: Session, *, sub_base: str, motoboy_id: int
) -> list[SaidaSessao]:
    return list(
        db.scalars(
            select(SaidaSessao)
            .where(
                SaidaSessao.sub_base == sub_base,
                SaidaSessao.motoboy_id == motoboy_id,
                SaidaSessao.estado == ESTADO_FINALIZADA,
                SaidaSessao.confirmada_em.is_(None),
            )
            .order_by(SaidaSessao.finalizada_em, SaidaSessao.id)
        ).all()
    )


def confirmar_sessoes_motoboy(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    sessao_ids: Iterable[int],
    ids_removidos: Iterable[int],
    user_id: Optional[int],
    chave_idempotente: Optional[str],
) -> dict:
    ids = {int(x) for x in sessao_ids}
    sessoes = list(
        db.scalars(
            select(SaidaSessao)
            .where(
                SaidaSessao.id.in_(ids),
                SaidaSessao.sub_base == sub_base,
                SaidaSessao.motoboy_id == motoboy_id,
            )
            .with_for_update()
        ).all()
    )
    if len(sessoes) != len(ids):
        raise HTTPException(404, detail={"code": "SESSAO_NAO_ENCONTRADA"})

    removidos = {int(x) for x in ids_removidos}
    now = datetime.utcnow()
    confirmados: list[int] = []
    for sessao in sessoes:
        if sessao.estado != ESTADO_FINALIZADA:
            raise HTTPException(409, detail={"code": "SESSAO_NAO_FINALIZADA"})
        if sessao.confirmada_em is not None:
            continue
        for item, saida in _itens_operacionais(db, sessao):
            if int(saida.id_saida) in removidos:
                item.ativo_operacional = False
                item.removido_em = now
                item.removido_por_user_id = user_id
                item.motivo_remocao = "REMOVIDO_ANTES_CONFIRMACAO"
                saida.motoboy_id = None
                _evento(
                    db,
                    saida,
                    "saida_removida_antes_confirmacao",
                    user_id=user_id,
                    payload={"sessao_id": int(sessao.id)},
                )
                continue
            anterior = saida.status
            saida.status = STATUS_SAIU_PARA_ENTREGA
            confirmados.append(int(saida.id_saida))
            _evento(
                db,
                saida,
                "saida_confirmada",
                user_id=user_id,
                status_anterior=anterior,
                status_novo=STATUS_SAIU_PARA_ENTREGA,
                payload={
                    "sessao_id": int(sessao.id),
                    "chave_idempotente": chave_idempotente,
                },
            )
        sessao.confirmada_em = now
        sessao.confirmada_por_user_id = user_id
        sessao.atualizada_em = now
    db.flush()
    return {
        "sessao_ids": sorted(ids),
        "ids_confirmados": confirmados,
        "ids_removidos": sorted(removidos),
        "total_confirmado": len(confirmados),
    }


def exigir_confirmacao_saidas(
    db: Session, *, sub_base: str, saida_ids: Iterable[int]
) -> None:
    if not fluxo_confirmacao_ativo(db, sub_base):
        return
    pendente = db.scalar(
        select(Saida.id_saida).where(
            Saida.id_saida.in_([int(x) for x in saida_ids]),
            Saida.sub_base == sub_base,
            Saida.status == STATUS_AGUARDANDO_CONFIRMACAO,
        ).limit(1)
    )
    if pendente is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SAIDA_NAO_CONFIRMADA",
                "message": "Confirme o início da entrega deste lote para continuar.",
            },
        )
