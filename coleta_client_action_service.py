"""Idempotência de leitura de coleta (PRD-020) via X-Client-Action-Id.

Claim-before-work: reserva a chave com `_pending` antes de criar Saida/cobrança.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from entrega_audit_log import norm_client_action_id
from models import ColetaClientAction

ClaimAction = Literal["replay", "in_progress", "proceed"]

_PENDING_MARKER = {"_pending": True}


def _parse_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_pending(data: Optional[dict[str, Any]]) -> bool:
    return bool(data and data.get("_pending") is True)


def buscar_resultado_idempotente(
    db: Session, *, sub_base: str, client_action_id: Optional[str]
) -> Optional[dict[str, Any]]:
    chave = norm_client_action_id(client_action_id)
    if not chave:
        return None
    row = db.scalar(
        select(ColetaClientAction).where(
            ColetaClientAction.sub_base == sub_base,
            ColetaClientAction.client_action_id == chave,
        )
    )
    if not row:
        return None
    data = _parse_json(row.resultado_json)
    if not data or _is_pending(data):
        return None
    return {**data, "idempotente": True}


def claim_client_action(
    db: Session,
    *,
    sub_base: str,
    client_action_id: Optional[str],
    base: Optional[str] = None,
    codigo: Optional[str] = None,
) -> Tuple[ClaimAction, Optional[dict[str, Any]]]:
    """
    Reserva a chave antes do trabalho.
    - replay: resultado pronto
    - in_progress: outro request ainda processando
    - proceed: esta sessão detém o claim
    Sem header → ("proceed", None) sem gravar.
    """
    chave = norm_client_action_id(client_action_id)
    if not chave:
        return "proceed", None

    row = db.scalar(
        select(ColetaClientAction).where(
            ColetaClientAction.sub_base == sub_base,
            ColetaClientAction.client_action_id == chave,
        )
    )
    if row is not None:
        data = _parse_json(row.resultado_json)
        if data and not _is_pending(data):
            return "replay", {**data, "idempotente": True}
        return "in_progress", None

    row = ColetaClientAction(
        sub_base=sub_base,
        client_action_id=chave,
        base=(base or "").strip() or None,
        codigo=(codigo or "").strip() or None,
        resultado_json=json.dumps(_PENDING_MARKER),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return "proceed", None
    except IntegrityError:
        existing = db.scalar(
            select(ColetaClientAction).where(
                ColetaClientAction.sub_base == sub_base,
                ColetaClientAction.client_action_id == chave,
            )
        )
        if existing is None:
            return "in_progress", None
        data = _parse_json(existing.resultado_json)
        if data and not _is_pending(data):
            return "replay", {**data, "idempotente": True}
        return "in_progress", None


def completar_client_action(
    db: Session,
    *,
    sub_base: str,
    client_action_id: Optional[str],
    resultado: dict[str, Any],
    base: Optional[str] = None,
    codigo: Optional[str] = None,
    id_saida: Optional[int] = None,
    id_coleta: Optional[int] = None,
    id_execucao: Optional[int] = None,
) -> None:
    chave = norm_client_action_id(client_action_id)
    if not chave:
        return
    row = db.scalar(
        select(ColetaClientAction).where(
            ColetaClientAction.sub_base == sub_base,
            ColetaClientAction.client_action_id == chave,
        )
    )
    payload = dict(resultado)
    payload.pop("idempotente", None)
    payload.pop("_pending", None)
    body = json.dumps(payload, default=str, ensure_ascii=False)
    if row is None:
        row = ColetaClientAction(
            sub_base=sub_base,
            client_action_id=chave,
            base=(base or "").strip() or None,
            codigo=(codigo or "").strip() or None,
            id_saida=id_saida,
            id_coleta=id_coleta,
            id_execucao=id_execucao,
            resultado_json=body,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            return
        return
    row.resultado_json = body
    if base:
        row.base = base.strip() or row.base
    if codigo:
        row.codigo = codigo.strip() or row.codigo
    if id_saida is not None:
        row.id_saida = id_saida
    if id_coleta is not None:
        row.id_coleta = id_coleta
    if id_execucao is not None:
        row.id_execucao = id_execucao


# Compat: nome antigo usado nos testes iniciais
def persistir_resultado_idempotente(
    db: Session,
    *,
    sub_base: str,
    client_action_id: Optional[str],
    resultado: dict[str, Any],
    base: Optional[str] = None,
    codigo: Optional[str] = None,
    id_saida: Optional[int] = None,
    id_coleta: Optional[int] = None,
    id_execucao: Optional[int] = None,
) -> None:
    completar_client_action(
        db,
        sub_base=sub_base,
        client_action_id=client_action_id,
        resultado=resultado,
        base=base,
        codigo=codigo,
        id_saida=id_saida,
        id_coleta=id_coleta,
        id_execucao=id_execucao,
    )
