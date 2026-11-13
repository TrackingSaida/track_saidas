"""Serviço transacional do ciclo de confirmação real de saída (PRD-012)."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    ConferenciaSaida,
    Motoboy,
    Owner,
    Saida,
    SaidaConfirmacaoIdempotencia,
    SaidaHistorico,
    SaidaSessao,
    SaidaSessaoItem,
)

STATUS_AGUARDANDO_CONFIRMACAO = "AGUARDANDO_CONFIRMACAO_SAIDA"
STATUS_SAIU_PARA_ENTREGA = "SAIU_PARA_ENTREGA"
ESTADO_ABERTA = "ABERTA"
ESTADO_FINALIZADA = "FINALIZADA"
ESTADO_CANCELADA = "CANCELADA"
MOTIVO_SEM_ITENS_VALIDOS = "SEM_ITENS_VALIDOS"
STATUS_FORA_DA_VISAO_OPERACIONAL = {"CANCELADO", "ENCERRADO", "ENCERRADO_SISTEMA"}


def _travar(stmt, db: Session):
    """Lock de linha no Postgres. SQLite de teste não implementa FOR UPDATE."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        return stmt
    return stmt.with_for_update()


def _exigir_versao(sessao: SaidaSessao, versao_esperada: Optional[int]) -> None:
    if versao_esperada is None:
        return
    atual = int(sessao.versao or 0)
    if atual != int(versao_esperada):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SESSAO_VERSAO_CONFLITO",
                "message": "A sessão mudou durante a leitura. Atualize a tela e tente de novo.",
                "versao": atual,
            },
        )


def _cancelar_sessao_sem_itens(sessao: SaidaSessao, now: datetime) -> None:
    sessao.estado = ESTADO_CANCELADA
    sessao.cancelada_em = now
    sessao.cancelada_por_user_id = None
    sessao.motivo_cancelamento = MOTIVO_SEM_ITENS_VALIDOS
    sessao.atualizada_em = now
    sessao.versao = int(sessao.versao or 0) + 1


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
        stmt = _travar(stmt, db)
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
        _travar(select(Motoboy).where(Motoboy.id_motoboy == motoboy_id), db)
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
        _travar(
            select(SaidaSessaoItem).where(
                SaidaSessaoItem.id_saida == id_saida,
                SaidaSessaoItem.ativo_operacional.is_(True),
            ),
            db,
        )
    ).all()
    now = datetime.utcnow()
    sessoes_afetadas: set[int] = set()
    for item in itens:
        item.ativo_operacional = False
        item.removido_em = now
        item.removido_por_user_id = user_id
        item.motivo_remocao = motivo
        item.sessao.versao = int(item.sessao.versao or 0) + 1
        item.sessao.atualizada_em = now
        sessoes_afetadas.add(int(item.sessao_id))
    for sessao_id in sessoes_afetadas:
        sessao = db.get(SaidaSessao, sessao_id)
        if sessao is None or sessao.estado != ESTADO_ABERTA:
            continue
        restantes = db.scalar(
            select(func.count(SaidaSessaoItem.id)).where(
                SaidaSessaoItem.sessao_id == sessao.id,
                SaidaSessaoItem.ativo_operacional.is_(True),
            )
        )
        if int(restantes or 0) == 0:
            _cancelar_sessao_sem_itens(sessao, now)
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
        _travar(
            select(SaidaSessaoItem).where(
                SaidaSessaoItem.id_saida == saida.id_saida,
                SaidaSessaoItem.ativo_operacional.is_(True),
            ),
            db,
        )
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


def contar_total_operacional(db: Session, sessao: Optional[SaidaSessao]) -> Optional[int]:
    """Total vivo dos itens ainda ativos. Um COUNT pontual, sem visão de conferência."""
    if sessao is None or sessao.id is None:
        return None
    total = db.scalar(
        select(func.count(SaidaSessaoItem.id)).where(
            SaidaSessaoItem.sessao_id == int(sessao.id),
            SaidaSessaoItem.ativo_operacional.is_(True),
        )
    )
    return int(total or 0)


def campos_sessao_leitura(db: Session, sessao: Optional[SaidaSessao]) -> dict:
    if sessao is None:
        return {"sessao_id": None, "sessao_status": None, "sessao_total": None, "sessao_versao": None}
    return {
        "sessao_id": int(sessao.id),
        "sessao_status": sessao.estado,
        "sessao_total": contar_total_operacional(db, sessao),
        "sessao_versao": int(sessao.versao or 0),
    }


def _motoboy_no_escopo(db: Session, *, sub_base: str, motoboy_id: int) -> Motoboy:
    motoboy = db.get(Motoboy, int(motoboy_id))
    if motoboy is None or (motoboy.sub_base or "") != sub_base:
        raise HTTPException(status_code=404, detail={"code": "MOTOBOY_NAO_ENCONTRADO"})
    owner_id = _owner_id(db, sub_base)
    if owner_id is None:
        raise HTTPException(status_code=404, detail={"code": "OWNER_NAO_ENCONTRADO"})
    return motoboy


def sessao_no_escopo(db: Session, sessao: Optional[SaidaSessao], *, sub_base: str) -> bool:
    if sessao is None or (sessao.sub_base or "") != sub_base:
        return False
    owner_id = _owner_id(db, sub_base)
    if owner_id is None:
        return False
    if sessao.owner_id is not None and int(sessao.owner_id) != int(owner_id):
        return False
    try:
        _motoboy_no_escopo(db, sub_base=sub_base, motoboy_id=int(sessao.motoboy_id))
    except HTTPException:
        return False
    return True


def carregar_sessao_no_escopo(db: Session, *, sessao_id: int, sub_base: str) -> SaidaSessao:
    sessao = db.scalar(
        select(SaidaSessao).where(
            SaidaSessao.id == int(sessao_id),
            SaidaSessao.sub_base == sub_base,
        )
    )
    if not sessao_no_escopo(db, sessao, sub_base=sub_base):
        raise HTTPException(status_code=404, detail={"code": "SESSAO_NAO_ENCONTRADA"})
    return sessao


def recuperar_sessao_aberta_staff(db: Session, *, sub_base: str, motoboy_id: int) -> dict:
    """Sessão aberta do motoboy no owner/sub_base do operador. Não vaza outro tenant."""
    _motoboy_no_escopo(db, sub_base=sub_base, motoboy_id=motoboy_id)
    ativo = fluxo_confirmacao_ativo(db, sub_base)
    if not ativo:
        return {
            "fluxo_confirmacao_ativo": False,
            "sessao_id": None,
            "sessao_status": None,
            "sessao_total": 0,
            "versao": None,
            "saida_ids": [],
        }
    sessao = obter_sessao_aberta(db, sub_base=sub_base, motoboy_id=int(motoboy_id))
    if not sessao_no_escopo(db, sessao, sub_base=sub_base):
        return {
            "fluxo_confirmacao_ativo": True,
            "sessao_id": None,
            "sessao_status": None,
            "sessao_total": 0,
            "versao": None,
            "saida_ids": [],
        }
    saida_ids = [
        int(x)
        for x in db.scalars(
            select(SaidaSessaoItem.id_saida).where(
                SaidaSessaoItem.sessao_id == sessao.id,
                SaidaSessaoItem.ativo_operacional.is_(True),
            )
        ).all()
    ]
    return {
        "fluxo_confirmacao_ativo": True,
        "sessao_id": int(sessao.id),
        "sessao_status": sessao.estado,
        "sessao_total": len(saida_ids),
        "versao": int(sessao.versao or 0),
        "saida_ids": saida_ids,
    }


def localizar_sessao_do_item(
    db: Session,
    *,
    id_saida: int,
    sub_base: str,
    motoboy_id: int,
) -> Optional[SaidaSessao]:
    """Consulta pontual da sessão do item. Não recalcula totais nem grava histórico."""
    return db.scalar(
        select(SaidaSessao)
        .join(SaidaSessaoItem, SaidaSessaoItem.sessao_id == SaidaSessao.id)
        .where(
            SaidaSessaoItem.id_saida == int(id_saida),
            SaidaSessaoItem.ativo_operacional.is_(True),
            SaidaSessao.sub_base == sub_base,
            SaidaSessao.motoboy_id == int(motoboy_id),
        )
        .limit(1)
    )


def entrar_nova_saida_mesmo_entregador(
    db: Session,
    *,
    saida: Saida,
    sub_base: str,
    motoboy_id: int,
    user_id: Optional[int],
    payload_historico: str,
) -> SaidaSessao:
    """Encerra o vínculo anterior e abre outro, na mesma transação."""
    anterior = saida.status
    motoboy_anterior = saida.motoboy_id
    invalidar_vinculo_operacional(
        db,
        id_saida=int(saida.id_saida),
        motivo="NOVA_SAIDA_MESMO_ENTREGADOR",
        user_id=user_id,
    )
    saida.status = STATUS_AGUARDANDO_CONFIRMACAO
    saida.motoboy_id = motoboy_id
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento="nova_saida_mesmo_entregador",
            status_anterior=anterior,
            status_novo=STATUS_AGUARDANDO_CONFIRMACAO,
            motoboy_id_anterior=motoboy_anterior,
            motoboy_id_novo=motoboy_id,
            user_id=user_id,
            payload=payload_historico,
        )
    )
    return incluir_saida_na_sessao(
        db,
        saida=saida,
        sub_base=sub_base,
        motoboy_id=int(motoboy_id),
        user_id=user_id,
        flag_ja_validada=True,
    )


def aplicar_nova_saida_mesmo_entregador(
    db: Session,
    *,
    saida: Saida,
    sub_base: str,
    user_id: Optional[int],
    payload_historico: str,
    fluxo_ativo: bool,
) -> Optional[SaidaSessao]:
    """Com a flag desligada, só registra o evento legado e mantém o status."""
    if not fluxo_ativo or saida.motoboy_id is None:
        anterior = saida.status
        db.add(
            SaidaHistorico(
                id_saida=saida.id_saida,
                evento="nova_saida_mesmo_entregador",
                status_anterior=anterior,
                status_novo=anterior,
                motoboy_id_anterior=saida.motoboy_id,
                motoboy_id_novo=saida.motoboy_id,
                user_id=user_id,
                payload=payload_historico,
            )
        )
        return None
    return entrar_nova_saida_mesmo_entregador(
        db,
        saida=saida,
        sub_base=sub_base,
        motoboy_id=int(saida.motoboy_id),
        user_id=user_id,
        payload_historico=payload_historico,
    )


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
    versao_esperada: Optional[int] = None,
) -> SaidaSessao:
    filtro_estado_ou_id = (
        SaidaSessao.id == sessao_id
        if sessao_id is not None
        else SaidaSessao.estado == ESTADO_ABERTA
    )
    stmt = _travar(
        select(SaidaSessao).where(
            SaidaSessao.sub_base == sub_base,
            SaidaSessao.motoboy_id == motoboy_id,
            filtro_estado_ou_id,
        ),
        db,
    )
    sessao = db.scalar(stmt)
    if sessao is None:
        raise HTTPException(404, detail={"code": "SESSAO_NAO_ENCONTRADA"})
    if sessao.estado == ESTADO_FINALIZADA:
        return sessao
    if sessao.estado != ESTADO_ABERTA:
        raise HTTPException(409, detail={"code": "SESSAO_NAO_ABERTA"})
    _exigir_versao(sessao, versao_esperada)

    itens = _itens_operacionais(db, sessao)
    now = datetime.utcnow()
    if not itens:
        _cancelar_sessao_sem_itens(sessao, now)
        db.flush()
        return sessao

    totais = {"shopee": 0, "ml": 0, "avulso": 0}
    for item, saida in itens:
        item.codigo_snapshot = saida.codigo
        item.servico_snapshot = saida.servico
        item.status_snapshot = saida.status
        totais[_bucket(item.servico_snapshot)] += 1
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
    sessao.versao = int(sessao.versao or 0) + 1
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


def _chave_idempotente(valor: Optional[str]) -> Optional[str]:
    chave = (valor or "").strip()
    return chave[:120] or None


def buscar_confirmacao_idempotente(
    db: Session, *, sub_base: str, motoboy_id: int, chave_idempotente: Optional[str]
) -> Optional[dict]:
    chave = _chave_idempotente(chave_idempotente)
    if not chave:
        return None
    row = db.scalar(
        select(SaidaConfirmacaoIdempotencia).where(
            SaidaConfirmacaoIdempotencia.sub_base == sub_base,
            SaidaConfirmacaoIdempotencia.motoboy_id == int(motoboy_id),
            SaidaConfirmacaoIdempotencia.chave == chave,
        )
    )
    if row is None or not row.resultado_json:
        return None
    data = json.loads(row.resultado_json)
    if isinstance(data, dict):
        data["idempotente"] = True
        return data
    return None


def _persistir_confirmacao_idempotente(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    chave: str,
    resultado: dict,
) -> None:
    payload = json.dumps(resultado, ensure_ascii=False)
    try:
        with db.begin_nested():
            db.add(
                SaidaConfirmacaoIdempotencia(
                    owner_id=_owner_id(db, sub_base),
                    sub_base=sub_base,
                    motoboy_id=int(motoboy_id),
                    chave=chave,
                    resultado_json=payload,
                )
            )
            db.flush()
    except IntegrityError:
        return


def confirmar_sessoes_motoboy(
    db: Session,
    *,
    sub_base: str,
    motoboy_id: int,
    sessao_ids: Iterable[int],
    ids_removidos: Iterable[int],
    user_id: Optional[int],
    chave_idempotente: Optional[str],
    versoes: Optional[dict] = None,
) -> dict:
    replay = buscar_confirmacao_idempotente(
        db,
        sub_base=sub_base,
        motoboy_id=motoboy_id,
        chave_idempotente=chave_idempotente,
    )
    if replay is not None:
        return replay

    ids = {int(x) for x in sessao_ids}
    sessoes = list(
        db.scalars(
            _travar(
                select(SaidaSessao).where(
                    SaidaSessao.id.in_(ids),
                    SaidaSessao.sub_base == sub_base,
                    SaidaSessao.motoboy_id == motoboy_id,
                ),
                db,
            )
        ).all()
    )
    if len(sessoes) != len(ids):
        raise HTTPException(404, detail={"code": "SESSAO_NAO_ENCONTRADA"})

    removidos = {int(x) for x in ids_removidos}
    versoes_norm = {int(k): int(v) for k, v in (versoes or {}).items()}
    now = datetime.utcnow()
    confirmados: list[int] = []
    ignorados: list[int] = []
    chave = _chave_idempotente(chave_idempotente)
    for sessao in sessoes:
        if sessao.estado != ESTADO_FINALIZADA:
            raise HTTPException(409, detail={"code": "SESSAO_NAO_FINALIZADA"})
        if int(sessao.id) in versoes_norm:
            _exigir_versao(sessao, versoes_norm[int(sessao.id)])
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
                    payload={"sessao_id": int(sessao.id), "chave_idempotente": chave},
                )
                continue
            if (saida.status or "") != STATUS_AGUARDANDO_CONFIRMACAO:
                ignorados.append(int(saida.id_saida))
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
                    "chave_idempotente": chave,
                },
            )
        sessao.confirmada_em = now
        sessao.confirmada_por_user_id = user_id
        sessao.atualizada_em = now
        sessao.versao = int(sessao.versao or 0) + 1
    db.flush()
    resultado = {
        "sessao_ids": sorted(ids),
        "ids_confirmados": confirmados,
        "ids_removidos": sorted(removidos),
        "ids_ignorados": sorted(set(ignorados)),
        "total_confirmado": len(confirmados),
        "idempotente": False,
    }
    if chave:
        _persistir_confirmacao_idempotente(
            db,
            sub_base=sub_base,
            motoboy_id=motoboy_id,
            chave=chave,
            resultado=resultado,
        )
    return resultado


def _item_no_snapshot(item: SaidaSessaoItem, sessao: SaidaSessao) -> bool:
    if sessao.finalizada_em is None:
        return bool(item.ativo_operacional)
    if item.removido_em is None:
        return True
    return item.removido_em >= sessao.finalizada_em


def _saida_na_visao_operacional(
    item: SaidaSessaoItem, saida: Optional[Saida], sessao: SaidaSessao
) -> bool:
    if not item.ativo_operacional or saida is None:
        return False
    if (saida.sub_base or "") != (sessao.sub_base or ""):
        return False
    if saida.motoboy_id is None or int(saida.motoboy_id) != int(sessao.motoboy_id):
        return False
    if (saida.status or "").strip().upper() in STATUS_FORA_DA_VISAO_OPERACIONAL:
        return False
    return True


def visoes_conferencia_sessao(db: Session, sessao: SaidaSessao) -> dict:
    """Snapshot congelado na finalização e visão operacional vigente."""
    rows = db.execute(
        select(SaidaSessaoItem, Saida)
        .outerjoin(Saida, Saida.id_saida == SaidaSessaoItem.id_saida)
        .where(SaidaSessaoItem.sessao_id == sessao.id)
        .order_by(SaidaSessaoItem.id)
    ).all()
    snapshot_ids: list[int] = []
    operacional: list[Saida] = []
    for item, saida in rows:
        if _item_no_snapshot(item, sessao):
            snapshot_ids.append(int(item.id_saida))
        if _saida_na_visao_operacional(item, saida, sessao):
            operacional.append(saida)
    totais = {"shopee": 0, "ml": 0, "avulso": 0}
    for saida in operacional:
        totais[_bucket(saida.servico)] += 1
    return {
        "snapshot_ids": snapshot_ids,
        "snapshot_total": int(sessao.total_snapshot or 0),
        "snapshot_shopee": int(sessao.total_shopee_snapshot or 0),
        "snapshot_ml": int(sessao.total_ml_snapshot or 0),
        "snapshot_avulso": int(sessao.total_avulso_snapshot or 0),
        "operacional_ids": [int(saida.id_saida) for saida in operacional],
        "operacional_total": len(operacional),
        "operacional_shopee": totais["shopee"],
        "operacional_ml": totais["ml"],
        "operacional_avulso": totais["avulso"],
    }


def exigir_confirmacao_saidas(
    db: Session, *, sub_base: str, saida_ids: Iterable[int]
) -> None:
    """Rejeita o lote inteiro se qualquer item ainda aguarda confirmação de saída."""
    ids = [int(x) for x in saida_ids]
    if not ids or not fluxo_confirmacao_ativo(db, sub_base):
        return
    rows = db.execute(
        select(Saida.id_saida, SaidaSessaoItem.sessao_id)
        .outerjoin(
            SaidaSessaoItem,
            (SaidaSessaoItem.id_saida == Saida.id_saida)
            & (SaidaSessaoItem.ativo_operacional.is_(True)),
        )
        .where(
            Saida.id_saida.in_(ids),
            Saida.sub_base == sub_base,
            Saida.status == STATUS_AGUARDANDO_CONFIRMACAO,
        )
    ).all()
    if not rows:
        return
    pendentes = sorted({int(id_saida) for id_saida, _sessao_id in rows})
    sessoes = sorted({int(sessao_id) for _id_saida, sessao_id in rows if sessao_id is not None})
    raise HTTPException(
        status_code=409,
        detail={
            "code": "SAIDA_NAO_CONFIRMADA",
            "message": "Confirme o início da entrega antes de continuar.",
            "sessao_ids": sessoes,
            "quantidade": len(pendentes),
            "ids_saida": pendentes,
        },
    )
