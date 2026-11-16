"""PRD-013 — transferência pendente de aceite quando o pacote está em rota ativa."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from active_route_sync import encerrar_item_da_operacao_anterior, pacote_em_rota_ativa
from models import Motoboy, Saida, SaidaDetail, SaidaHistorico, SaidaTransferenciaPendente, User

logger = logging.getLogger(__name__)

CODE_AGUARDANDO = "TRANSFERENCIA_AGUARDANDO_ACEITE"
CODE_JA_RESOLVIDA = "TRANSFERENCIA_JA_RESOLVIDA"
CODE_OBSOLETA = "TRANSFERENCIA_OBSOLETA"
MSG_AGUARDANDO = (
    "Este pedido está na rota iniciada do entregador atual. "
    "A transferência aguarda o aceite dele."
)

STATUS_PENDENTE = "pendente"
STATUS_ACEITA = "aceita"
STATUS_RECUSADA = "recusada"
STATUS_CANCELADA = "cancelada"
STATUS_OBSOLETA = "obsoleta"

STATUS_SAIU = "SAIU_PARA_ENTREGA"
STATUS_EM_ROTA = "EM_ROTA"
STATUS_AGUARDANDO = "AGUARDANDO_CONFIRMACAO_SAIDA"
STATUS_ENTREGUE = "ENTREGUE"
STATUS_AUSENTE = "AUSENTE"
STATUS_ENVIADO_PARA_BASE = "ENVIADO_PARA_BASE"


def _now() -> datetime:
    return datetime.utcnow()


def _raise_aguardando(transferencia_id: int) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": CODE_AGUARDANDO,
            "transferencia_id": int(transferencia_id),
            "message": MSG_AGUARDANDO,
        },
    )


def _raise_ja_resolvida(status: str) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": CODE_JA_RESOLVIDA,
            "status": status,
            "message": "Esta solicitação de transferência já foi resolvida.",
        },
    )


def _raise_obsoleta(message: str = "A solicitação de transferência não é mais válida.") -> None:
    raise HTTPException(
        status_code=409,
        detail={"code": CODE_OBSOLETA, "message": message},
    )


def pendencia_ativa_da_saida(db: Session, id_saida: int) -> Optional[SaidaTransferenciaPendente]:
    return db.scalar(
        select(SaidaTransferenciaPendente)
        .where(
            SaidaTransferenciaPendente.id_saida == int(id_saida),
            SaidaTransferenciaPendente.status == STATUS_PENDENTE,
        )
        .limit(1)
    )


def listar_pendentes_origem(
    db: Session,
    *,
    motoboy_id: int,
    sub_base: str,
) -> List[SaidaTransferenciaPendente]:
    return list(
        db.scalars(
            select(SaidaTransferenciaPendente)
            .where(
                SaidaTransferenciaPendente.motoboy_origem_id == int(motoboy_id),
                SaidaTransferenciaPendente.sub_base == sub_base,
                SaidaTransferenciaPendente.status == STATUS_PENDENTE,
            )
            .order_by(SaidaTransferenciaPendente.criada_em.asc())
        ).all()
    )


def pendentes_resumo_origem(
    db: Session,
    *,
    motoboy_id: int,
    sub_base: str,
) -> List[Dict[str, Any]]:
    rows = listar_pendentes_origem(db, motoboy_id=motoboy_id, sub_base=sub_base)
    out: List[Dict[str, Any]] = []
    for row in rows:
        saida = db.get(Saida, int(row.id_saida))
        out.append(
            {
                "transferencia_id": int(row.id),
                "id_saida": int(row.id_saida),
                "codigo": getattr(saida, "codigo", None),
                "rota_id": str(row.rota_id) if row.rota_id is not None else None,
                "motoboy_destino_id": int(row.motoboy_destino_id)
                if row.motoboy_destino_id is not None
                else None,
                "criada_em": row.criada_em.isoformat() if row.criada_em else None,
            }
        )
    return out


def _push_origem(
    db: Session,
    *,
    motoboy_id: int,
    sub_base: str,
    transferencia_id: int,
    id_saida: int,
    codigo: Optional[str],
) -> None:
    try:
        from push_notification_service import send_to_motoboy

        send_to_motoboy(
            db,
            motoboy_id=int(motoboy_id),
            sub_base=sub_base,
            tipo="transferencia_pendente",
            title="Transferência de pedido",
            body=(
                f"Pedido {codigo or id_saida} pode sair da sua rota. "
                "As paradas podem mudar se você aceitar."
            ),
            data={
                "tipo": "transferencia_pendente",
                "transferencia_id": int(transferencia_id),
                "id_saida": int(id_saida),
            },
            chave_dedupe=f"transferencia_pendente:{transferencia_id}",
        )
    except Exception:
        logger.exception(
            "push_transferencia_pendente_failed transferencia_id=%s", transferencia_id
        )


def _push_resolucao(
    db: Session,
    *,
    motoboy_id: Optional[int],
    sub_base: str,
    transferencia_id: int,
    tipo: str,
    title: str,
    body: str,
) -> None:
    if motoboy_id is None:
        return
    try:
        from push_notification_service import send_to_motoboy

        send_to_motoboy(
            db,
            motoboy_id=int(motoboy_id),
            sub_base=sub_base,
            tipo=tipo,
            title=title,
            body=body,
            data={"tipo": tipo, "transferencia_id": int(transferencia_id)},
            chave_dedupe=f"{tipo}:{transferencia_id}",
        )
    except Exception:
        logger.exception("push_transferencia_resolucao_failed tipo=%s id=%s", tipo, transferencia_id)


def solicitar_reatribuicao(
    db: Session,
    *,
    saida: Saida,
    sub_base: str,
    motoboy_destino_id: Optional[int],
    repasse_base_id: Optional[int] = None,
    solicitante_user_id: Optional[int] = None,
    solicitante_motoboy_id: Optional[int] = None,
    raise_conflict: bool = True,
) -> SaidaTransferenciaPendente:
    """Cria/atualiza pendência quando o pacote está na rota ativa do titular.

    Não altera posse. Se raise_conflict, dispara HTTP 409 após persistir a pendência
    (o caller deve fazer commit antes ou deixar o raise abortar a request — usamos
    flush + raise; commit fica a cargo do caller via exception handler ou commit prévio).
    """
    origem_id = getattr(saida, "motoboy_id", None)
    if origem_id is None:
        raise HTTPException(status_code=422, detail="Pedido sem entregador atual.")

    rota = pacote_em_rota_ativa(
        db,
        motoboy_id=int(origem_id),
        id_saida=int(saida.id_saida),
        sub_base=sub_base,
    )
    if rota is None:
        raise HTTPException(
            status_code=422,
            detail="Pedido não está em rota iniciada; use a reatribuição imediata.",
        )

    if motoboy_destino_id is not None and int(motoboy_destino_id) == int(origem_id):
        raise HTTPException(status_code=422, detail="Destino igual ao entregador atual.")

    existing = pendencia_ativa_da_saida(db, int(saida.id_saida))
    now = _now()
    if existing:
        existing.motoboy_destino_id = (
            int(motoboy_destino_id) if motoboy_destino_id is not None else None
        )
        existing.repasse_base_id = (
            int(repasse_base_id) if repasse_base_id is not None else None
        )
        existing.solicitante_user_id = solicitante_user_id
        existing.solicitante_motoboy_id = (
            int(solicitante_motoboy_id) if solicitante_motoboy_id is not None else None
        )
        existing.rota_id = int(rota.id)
        existing.route_revision_ref = int(getattr(rota, "route_revision", 0) or 0)
        existing.criada_em = now
        row = existing
    else:
        row = SaidaTransferenciaPendente(
            id_saida=int(saida.id_saida),
            sub_base=sub_base,
            motoboy_origem_id=int(origem_id),
            motoboy_destino_id=(
                int(motoboy_destino_id) if motoboy_destino_id is not None else None
            ),
            repasse_base_id=int(repasse_base_id) if repasse_base_id is not None else None,
            solicitante_user_id=solicitante_user_id,
            solicitante_motoboy_id=(
                int(solicitante_motoboy_id) if solicitante_motoboy_id is not None else None
            ),
            rota_id=int(rota.id),
            route_revision_ref=int(getattr(rota, "route_revision", 0) or 0),
            status=STATUS_PENDENTE,
            criada_em=now,
        )
        db.add(row)

    db.add(
        SaidaHistorico(
            id_saida=int(saida.id_saida),
            evento="transferencia_solicitada",
            status_anterior=saida.status,
            status_novo=saida.status,
            motoboy_id_anterior=int(origem_id),
            motoboy_id_novo=(
                int(motoboy_destino_id) if motoboy_destino_id is not None else None
            ),
            user_id=solicitante_user_id,
            payload=json.dumps(
                {
                    "rota_id": int(rota.id),
                    "repasse_base_id": int(repasse_base_id)
                    if repasse_base_id is not None
                    else None,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.flush()
    _push_origem(
        db,
        motoboy_id=int(origem_id),
        sub_base=sub_base,
        transferencia_id=int(row.id),
        id_saida=int(saida.id_saida),
        codigo=getattr(saida, "codigo", None),
    )
    db.commit()
    db.refresh(row)
    if raise_conflict:
        _raise_aguardando(int(row.id))
    return row


def tentar_gate_reatribuicao(
    db: Session,
    *,
    saida: Saida,
    sub_base: str,
    motoboy_destino_id: Optional[int],
    repasse_base_id: Optional[int] = None,
    solicitante_user_id: Optional[int] = None,
    solicitante_motoboy_id: Optional[int] = None,
) -> bool:
    """Se o pacote está em rota ativa, solicita pendência e levanta 409. Retorna False se sem gate."""
    origem_id = getattr(saida, "motoboy_id", None)
    if origem_id is None:
        return False
    rota = pacote_em_rota_ativa(
        db,
        motoboy_id=int(origem_id),
        id_saida=int(saida.id_saida),
        sub_base=sub_base,
    )
    if rota is None:
        return False
    solicitar_reatribuicao(
        db,
        saida=saida,
        sub_base=sub_base,
        motoboy_destino_id=motoboy_destino_id,
        repasse_base_id=repasse_base_id,
        solicitante_user_id=solicitante_user_id,
        solicitante_motoboy_id=solicitante_motoboy_id,
        raise_conflict=True,
    )
    return True  # unreachable when raise_conflict


def _motoboy_nome(db: Session, motoboy: Motoboy) -> str:
    from saidas_routes import _get_motoboy_nome

    return _get_motoboy_nome(db, motoboy)


def aplicar_reatribuicao_apos_aceite(
    db: Session,
    *,
    saida: Saida,
    transferencia: SaidaTransferenciaPendente,
    resolvida_por_user_id: Optional[int],
) -> Dict[str, Any]:
    """Aplica troca de titular + remove da rota do origem."""
    from saida_sessao_service import fluxo_confirmacao_ativo, incluir_saida_na_sessao
    from saidas_routes import (
        STATUS_AGUARDANDO_CONFIRMACAO_SAIDA,
        STATUS_EM_ROTA,
        STATUS_SAIU_PARA_ENTREGA,
        _aplicar_detail_reatribuicao_entregue,
        normalizar_status_saida,
    )

    sub_base = transferencia.sub_base
    origem_id = int(transferencia.motoboy_origem_id)
    status_norm = normalizar_status_saida(saida.status)
    status_anterior = saida.status

    encerrar_item_da_operacao_anterior(
        db,
        sub_base=sub_base,
        motoboy_id=origem_id,
        id_saida=int(saida.id_saida),
        motivo="REATRIBUIDO",
        user_id=resolvida_por_user_id,
    )

    if transferencia.repasse_base_id is not None and transferencia.motoboy_destino_id is None:
        if not hasattr(saida, "repasse_base_id"):
            _raise_obsoleta("Repasse para base não disponível neste ambiente.")
        saida.motoboy_id = None
        saida.entregador_id = None
        saida.entregador = None
        saida.repasse_base_id = int(transferencia.repasse_base_id)
        saida.status = STATUS_ENVIADO_PARA_BASE
        status_novo = STATUS_ENVIADO_PARA_BASE
        evento = "enviado_para_base"
        motoboy_novo = None
    else:
        dest_id = transferencia.motoboy_destino_id
        if dest_id is None:
            _raise_obsoleta("Destino da transferência inválido.")
        motoboy = db.get(Motoboy, int(dest_id))
        if not motoboy:
            _raise_obsoleta("Motoboy destino não encontrado.")
        saida.motoboy_id = int(motoboy.id_motoboy)
        if hasattr(saida, "repasse_base_id"):
            saida.repasse_base_id = None
        saida.entregador = _motoboy_nome(db, motoboy)
        saida.entregador_id = None
        fluxo = fluxo_confirmacao_ativo(db, sub_base)
        if status_norm == STATUS_ENTREGUE:
            saida.data_hora_entrega = None
            if fluxo:
                saida.status = STATUS_AGUARDANDO_CONFIRMACAO_SAIDA
            else:
                saida.status = STATUS_EM_ROTA
            _aplicar_detail_reatribuicao_entregue(
                db, int(saida.id_saida), int(motoboy.id_motoboy), saida.status
            )
        elif fluxo:
            saida.status = STATUS_AGUARDANDO_CONFIRMACAO_SAIDA
        else:
            saida.status = STATUS_SAIU_PARA_ENTREGA
        status_novo = saida.status
        if fluxo:
            incluir_saida_na_sessao(
                db,
                saida=saida,
                sub_base=sub_base,
                motoboy_id=int(motoboy.id_motoboy),
                user_id=resolvida_por_user_id,
                flag_ja_validada=True,
            )
        evento = "reatribuido"
        motoboy_novo = int(motoboy.id_motoboy)

    transferencia.status = STATUS_ACEITA
    transferencia.resolvida_em = _now()
    transferencia.resolvida_por_user_id = resolvida_por_user_id

    db.add(
        SaidaHistorico(
            id_saida=int(saida.id_saida),
            evento=evento,
            status_anterior=status_anterior,
            status_novo=status_novo,
            motoboy_id_anterior=origem_id,
            motoboy_id_novo=motoboy_novo,
            user_id=resolvida_por_user_id,
            payload=json.dumps(
                {"transferencia_id": int(transferencia.id)}, ensure_ascii=False
            ),
        )
    )

    if motoboy_novo is not None:
        try:
            from push_notification_service import enqueue_pacotes_atribuidos

            enqueue_pacotes_atribuidos(
                db,
                motoboy_id=motoboy_novo,
                sub_base=sub_base,
                codigo=getattr(saida, "codigo", None) or str(saida.id_saida),
            )
        except Exception:
            logger.exception("enqueue_pacotes_apos_aceite_failed id_saida=%s", saida.id_saida)

    _push_resolucao(
        db,
        motoboy_id=transferencia.solicitante_motoboy_id,
        sub_base=sub_base,
        transferencia_id=int(transferencia.id),
        tipo="transferencia_resolvida",
        title="Transferência aceita",
        body=f"O entregador aceitou a transferência do pedido {getattr(saida, 'codigo', saida.id_saida)}.",
    )
    return {
        "ok": True,
        "transferencia_id": int(transferencia.id),
        "id_saida": int(saida.id_saida),
        "status": STATUS_ACEITA,
        "motoboy_id": motoboy_novo,
    }


def aceitar_transferencia(
    db: Session,
    *,
    transferencia_id: int,
    motoboy_id: int,
    sub_base: str,
    user_id: Optional[int],
) -> Dict[str, Any]:
    row = db.get(SaidaTransferenciaPendente, int(transferencia_id))
    if not row or row.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Transferência não encontrada.")
    if row.status != STATUS_PENDENTE:
        _raise_ja_resolvida(row.status)
    if int(row.motoboy_origem_id) != int(motoboy_id):
        raise HTTPException(status_code=403, detail="Somente o entregador atual pode aceitar.")

    saida = db.get(Saida, int(row.id_saida))
    if not saida or saida.sub_base != sub_base:
        row.status = STATUS_OBSOLETA
        row.resolvida_em = _now()
        row.resolvida_por_user_id = user_id
        db.commit()
        _raise_obsoleta("Pedido não encontrado.")

    if saida.motoboy_id is None or int(saida.motoboy_id) != int(motoboy_id):
        row.status = STATUS_OBSOLETA
        row.resolvida_em = _now()
        row.resolvida_por_user_id = user_id
        db.commit()
        _raise_obsoleta("Você não é mais o titular deste pedido.")

    status_u = (saida.status or "").strip().upper()
    if status_u in (STATUS_ENTREGUE, STATUS_AUSENTE, "CANCELADO", "CANCELADA"):
        row.status = STATUS_OBSOLETA
        row.resolvida_em = _now()
        row.resolvida_por_user_id = user_id
        db.commit()
        _raise_obsoleta("O pedido já foi finalizado.")

    result = aplicar_reatribuicao_apos_aceite(
        db,
        saida=saida,
        transferencia=row,
        resolvida_por_user_id=user_id,
    )
    db.commit()
    return result


def recusar_transferencia(
    db: Session,
    *,
    transferencia_id: int,
    motoboy_id: int,
    sub_base: str,
    user_id: Optional[int],
) -> Dict[str, Any]:
    row = db.get(SaidaTransferenciaPendente, int(transferencia_id))
    if not row or row.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Transferência não encontrada.")
    if row.status != STATUS_PENDENTE:
        _raise_ja_resolvida(row.status)
    if int(row.motoboy_origem_id) != int(motoboy_id):
        raise HTTPException(status_code=403, detail="Somente o entregador atual pode recusar.")

    saida = db.get(Saida, int(row.id_saida))
    row.status = STATUS_RECUSADA
    row.resolvida_em = _now()
    row.resolvida_por_user_id = user_id
    db.add(
        SaidaHistorico(
            id_saida=int(row.id_saida),
            evento="transferencia_recusada",
            status_anterior=getattr(saida, "status", None),
            status_novo=getattr(saida, "status", None),
            motoboy_id_anterior=int(row.motoboy_origem_id),
            motoboy_id_novo=int(row.motoboy_destino_id)
            if row.motoboy_destino_id is not None
            else None,
            user_id=user_id,
            payload=json.dumps({"transferencia_id": int(row.id)}, ensure_ascii=False),
        )
    )
    _push_resolucao(
        db,
        motoboy_id=row.solicitante_motoboy_id,
        sub_base=sub_base,
        transferencia_id=int(row.id),
        tipo="transferencia_recusada",
        title="Transferência recusada",
        body="O entregador recusou a transferência do pedido.",
    )
    db.commit()
    return {
        "ok": True,
        "transferencia_id": int(row.id),
        "id_saida": int(row.id_saida),
        "status": STATUS_RECUSADA,
    }


def cancelar_transferencia(
    db: Session,
    *,
    transferencia_id: int,
    sub_base: str,
    user: User,
    is_staff: bool,
) -> Dict[str, Any]:
    row = db.get(SaidaTransferenciaPendente, int(transferencia_id))
    if not row or row.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Transferência não encontrada.")
    if row.status != STATUS_PENDENTE:
        _raise_ja_resolvida(row.status)

    uid = getattr(user, "id", None)
    mid = getattr(user, "motoboy_id", None)
    is_solicitante = False
    if uid is not None and row.solicitante_user_id is not None and int(uid) == int(row.solicitante_user_id):
        is_solicitante = True
    if mid is not None and row.solicitante_motoboy_id is not None and int(mid) == int(row.solicitante_motoboy_id):
        is_solicitante = True
    if not is_staff and not is_solicitante:
        raise HTTPException(status_code=403, detail="Sem permissão para cancelar esta transferência.")

    saida = db.get(Saida, int(row.id_saida))
    row.status = STATUS_CANCELADA
    row.resolvida_em = _now()
    row.resolvida_por_user_id = uid
    db.add(
        SaidaHistorico(
            id_saida=int(row.id_saida),
            evento="transferencia_cancelada",
            status_anterior=getattr(saida, "status", None),
            status_novo=getattr(saida, "status", None),
            motoboy_id_anterior=int(row.motoboy_origem_id),
            motoboy_id_novo=int(row.motoboy_destino_id)
            if row.motoboy_destino_id is not None
            else None,
            user_id=uid,
            payload=json.dumps({"transferencia_id": int(row.id)}, ensure_ascii=False),
        )
    )
    _push_resolucao(
        db,
        motoboy_id=int(row.motoboy_origem_id),
        sub_base=sub_base,
        transferencia_id=int(row.id),
        tipo="transferencia_resolvida",
        title="Transferência cancelada",
        body="A solicitação de transferência foi cancelada.",
    )
    db.commit()
    return {
        "ok": True,
        "transferencia_id": int(row.id),
        "id_saida": int(row.id_saida),
        "status": STATUS_CANCELADA,
    }


def obsolecer_pendencias_da_saida(
    db: Session,
    *,
    id_saida: int,
    user_id: Optional[int] = None,
) -> int:
    rows = list(
        db.scalars(
            select(SaidaTransferenciaPendente).where(
                SaidaTransferenciaPendente.id_saida == int(id_saida),
                SaidaTransferenciaPendente.status == STATUS_PENDENTE,
            )
        ).all()
    )
    now = _now()
    for row in rows:
        row.status = STATUS_OBSOLETA
        row.resolvida_em = now
        row.resolvida_por_user_id = user_id
    return len(rows)
