"""
Serviço de envio de push via Expo Push API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import DevicePushToken, NotifPrefs, PushDigest, PushEnvioLog

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
# IDs novos forçam recriação do canal no Android (importância/som atualizados)
CHANNEL_DEFAULT = "avisos_geral"
CHANNEL_URGENT = "avisos_urgente"

PREF_FECHAMENTO = "fechamento"
PREF_PACOTES = "pacotes_atribuidos"
PREF_ATRASO = "atraso_d1"
PREF_AVISOS = "avisos_base"
PREF_RECONFERIR = "reconferir_saida"

# Tipos operacionais que ignoram opt-out do motoboy (prefs não são mais editáveis no app)
ALWAYS_SEND_TYPES = frozenset(
    {
        "bloqueio_ausencia",
        "aviso_urgente",
        "aviso_base",
        "fechamento_pronto",
        "fechamento_reajustado",
    }
)


def _now() -> datetime:
    return datetime.utcnow()


def get_or_create_prefs(
    db: Session,
    *,
    user_id: int,
    sub_base: str,
    motoboy_id: Optional[int] = None,
) -> NotifPrefs:
    row = db.scalar(
        select(NotifPrefs).where(
            NotifPrefs.user_id == user_id,
            NotifPrefs.sub_base == sub_base,
        )
    )
    if row:
        if motoboy_id is not None and row.motoboy_id != motoboy_id:
            row.motoboy_id = motoboy_id
        return row
    row = NotifPrefs(
        user_id=user_id,
        motoboy_id=motoboy_id,
        sub_base=sub_base,
    )
    db.add(row)
    db.flush()
    return row


def pref_allows(prefs: Optional[NotifPrefs], tipo: str) -> bool:
    if tipo in ALWAYS_SEND_TYPES:
        return True
    if prefs is None:
        return True
    mapping = {
        "fechamento_pronto": prefs.fechamento,
        "fechamento_reajustado": prefs.fechamento,
        "pacotes_atribuidos": prefs.pacotes_atribuidos,
        "atraso_d1": prefs.atraso_d1,
        "aviso_base": prefs.avisos_base,
        "reconferir_saida": prefs.reconferir_saida,
    }
    return bool(mapping.get(tipo, True))


def already_sent(
    db: Session,
    *,
    destinatario_tipo: str,
    destinatario_id: int,
    sub_base: str,
    tipo: str,
    chave_dedupe: str,
) -> bool:
    exists = db.scalar(
        select(PushEnvioLog.id).where(
            PushEnvioLog.destinatario_tipo == destinatario_tipo,
            PushEnvioLog.destinatario_id == destinatario_id,
            PushEnvioLog.sub_base == sub_base,
            PushEnvioLog.tipo == tipo,
            PushEnvioLog.chave_dedupe == chave_dedupe,
        )
    )
    return exists is not None


def mark_sent(
    db: Session,
    *,
    destinatario_tipo: str,
    destinatario_id: int,
    sub_base: str,
    tipo: str,
    chave_dedupe: str,
) -> None:
    db.add(
        PushEnvioLog(
            destinatario_tipo=destinatario_tipo,
            destinatario_id=destinatario_id,
            sub_base=sub_base,
            tipo=tipo,
            chave_dedupe=chave_dedupe,
        )
    )


def _build_message(
    token: str,
    *,
    title: str,
    body: str,
    data: Dict[str, Any],
    tipo: str,
) -> Dict[str, Any]:
    is_high = tipo in (
        "aviso_urgente",
        "aviso_base",
        "fechamento_pronto",
        "fechamento_reajustado",
        "bloqueio_ausencia",
    )
    channel = CHANNEL_URGENT if tipo == "aviso_urgente" else CHANNEL_DEFAULT
    return {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
        "channelId": channel,
        # Prioridade alta ajuda o Android a tocar som de forma confiável
        "priority": "high" if is_high else "default",
        "data": {**data, "type": tipo},
    }


def _send_expo(messages: List[Dict[str, Any]]) -> None:
    if not messages:
        return
    try:
        # Expo aceita até 100 por request
        for i in range(0, len(messages), 100):
            chunk = messages[i : i + 100]
            resp = requests.post(EXPO_PUSH_URL, json=chunk, timeout=15)
            if resp.status_code >= 400:
                logger.warning("expo_push_http_error status=%s body=%s", resp.status_code, resp.text[:300])
                continue
            payload = resp.json()
            tickets = payload.get("data") or []
            for idx, ticket in enumerate(tickets):
                if isinstance(ticket, dict) and ticket.get("status") == "error":
                    details = ticket.get("details") or {}
                    if details.get("error") == "DeviceNotRegistered":
                        bad_token = chunk[idx].get("to")
                        logger.info("expo_token_invalid token=%s", bad_token)
    except Exception:
        logger.exception("expo_push_send_failed count=%s", len(messages))


def _deactivate_invalid_tokens(db: Session, tokens: Sequence[str]) -> None:
    if not tokens:
        return
    rows = db.scalars(
        select(DevicePushToken).where(DevicePushToken.expo_push_token.in_(list(tokens)))
    ).all()
    for row in rows:
        row.ativo = False
        row.atualizado_em = _now()


def send_to_motoboy(
    db: Session,
    *,
    motoboy_id: int,
    sub_base: str,
    tipo: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    chave_dedupe: Optional[str] = None,
) -> int:
    """Envia push para todos os tokens ativos do motoboy na sub_base. Retorna qtd de mensagens."""
    data = dict(data or {})
    if chave_dedupe and already_sent(
        db,
        destinatario_tipo="motoboy",
        destinatario_id=motoboy_id,
        sub_base=sub_base,
        tipo=tipo,
        chave_dedupe=chave_dedupe,
    ):
        return 0

    tokens = db.scalars(
        select(DevicePushToken).where(
            DevicePushToken.motoboy_id == motoboy_id,
            DevicePushToken.sub_base == sub_base,
            DevicePushToken.ativo.is_(True),
        )
    ).all()
    if not tokens:
        return 0

    # Prefs: usa a do primeiro user_id com token (geralmente o mesmo)
    prefs = db.scalar(
        select(NotifPrefs).where(
            NotifPrefs.motoboy_id == motoboy_id,
            NotifPrefs.sub_base == sub_base,
        )
    )
    if prefs is None and tokens:
        prefs = db.scalar(
            select(NotifPrefs).where(
                NotifPrefs.user_id == tokens[0].user_id,
                NotifPrefs.sub_base == sub_base,
            )
        )
    if not pref_allows(prefs, tipo):
        return 0

    messages = [
        _build_message(t.expo_push_token, title=title, body=body, data=data, tipo=tipo)
        for t in tokens
        if t.expo_push_token
    ]
    _send_expo(messages)

    if chave_dedupe:
        mark_sent(
            db,
            destinatario_tipo="motoboy",
            destinatario_id=motoboy_id,
            sub_base=sub_base,
            tipo=tipo,
            chave_dedupe=chave_dedupe,
        )
    return len(messages)


def send_to_staff_sub_base(
    db: Session,
    *,
    sub_base: str,
    tipo: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    roles: Sequence[int] = (0, 1, 2, 3),
    exclude_user_id: Optional[int] = None,
) -> int:
    data = dict(data or {})
    q = select(DevicePushToken).where(
        DevicePushToken.sub_base == sub_base,
        DevicePushToken.ativo.is_(True),
        DevicePushToken.role.in_(list(roles)),
        DevicePushToken.motoboy_id.is_(None),
    )
    tokens = db.scalars(q).all()
    if exclude_user_id is not None:
        tokens = [t for t in tokens if t.user_id != exclude_user_id]
    if not tokens:
        return 0

    messages: List[Dict[str, Any]] = []
    for t in tokens:
        prefs = db.scalar(
            select(NotifPrefs).where(
                NotifPrefs.user_id == t.user_id,
                NotifPrefs.sub_base == sub_base,
            )
        )
        if not pref_allows(prefs, tipo):
            continue
        messages.append(
            _build_message(t.expo_push_token, title=title, body=body, data=data, tipo=tipo)
        )
    _send_expo(messages)
    return len(messages)


def enqueue_pacotes_atribuidos(
    db: Session,
    *,
    motoboy_id: int,
    sub_base: str,
    codigo: Optional[str] = None,
    delay_seconds: int = 60,
) -> None:
    now = _now()
    row = db.scalar(
        select(PushDigest).where(
            PushDigest.motoboy_id == motoboy_id,
            PushDigest.sub_base == sub_base,
            PushDigest.tipo == "pacotes_atribuidos",
        )
    )
    if row:
        row.count = int(row.count or 0) + 1
        if codigo:
            row.last_codigo = codigo
        row.flush_after = now + timedelta(seconds=delay_seconds)
        row.atualizado_em = now
    else:
        db.add(
            PushDigest(
                motoboy_id=motoboy_id,
                sub_base=sub_base,
                tipo="pacotes_atribuidos",
                count=1,
                last_codigo=codigo,
                flush_after=now + timedelta(seconds=delay_seconds),
            )
        )


def flush_push_digests(db: Session) -> Dict[str, int]:
    now = _now()
    rows = db.scalars(
        select(PushDigest).where(PushDigest.flush_after <= now)
    ).all()
    sent = 0
    for row in rows:
        count = int(row.count or 0)
        if count <= 0:
            db.delete(row)
            continue
        if count == 1 and row.last_codigo:
            title = "Novo pacote atribuído"
            body = f"Pacote {row.last_codigo} foi atribuído a você"
        else:
            title = "Novos pacotes atribuídos"
            body = f"{count} novos pacotes atribuídos a você"
        n = send_to_motoboy(
            db,
            motoboy_id=row.motoboy_id,
            sub_base=row.sub_base,
            tipo="pacotes_atribuidos",
            title=title,
            body=body,
            data={"count": count, "codigo": row.last_codigo},
        )
        sent += n
        db.delete(row)
    db.commit()
    return {"digests": len(rows), "messages": sent}
