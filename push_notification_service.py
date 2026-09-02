"""
Serviço de envio de push via Expo Push API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models import DevicePushToken, Motoboy, NotifPrefs, PushDigest, PushEnvioLog

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
        "fechamento_pago",
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
        "fechamento_pago": prefs.fechamento,
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
        "fechamento_pago",
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


def _send_expo(messages: List[Dict[str, Any]]) -> List[str]:
    """Envia mensagens à Expo Push API. Retorna tokens inválidos (DeviceNotRegistered)."""
    invalid: List[str] = []
    if not messages:
        return invalid
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
                    err = details.get("error")
                    bad_token = chunk[idx].get("to")
                    logger.warning(
                        "expo_push_ticket_error error=%s message=%s token=%s",
                        err,
                        ticket.get("message"),
                        bad_token,
                    )
                    if err == "DeviceNotRegistered" and bad_token:
                        invalid.append(str(bad_token))
                elif isinstance(ticket, dict) and ticket.get("status") == "ok":
                    logger.info("expo_push_ticket_ok id=%s", ticket.get("id"))
    except Exception:
        logger.exception("expo_push_send_failed count=%s", len(messages))
    return invalid


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

    tokens = list(
        db.scalars(
            select(DevicePushToken).where(
                DevicePushToken.motoboy_id == motoboy_id,
                DevicePushToken.sub_base == sub_base,
                DevicePushToken.ativo.is_(True),
            )
        ).all()
    )
    motoboy = db.get(Motoboy, motoboy_id)
    user_id = int(motoboy.user_id) if motoboy and motoboy.user_id else None

    # Fallback 1: token com user_id na mesma sub_base (JWT sem motoboy_id no register)
    if not tokens and user_id:
        tokens = list(
            db.scalars(
                select(DevicePushToken).where(
                    DevicePushToken.user_id == user_id,
                    DevicePushToken.sub_base == sub_base,
                    DevicePushToken.ativo.is_(True),
                )
            ).all()
        )
        if tokens:
            logger.info(
                "push_token_recuperado_via_user_id tipo=%s motoboy_id=%s user_id=%s qtd=%s",
                tipo,
                motoboy_id,
                user_id,
                len(tokens),
            )

    # Fallback 2: mesmo motoboy em outra sub_base (login/troca de base)
    if not tokens:
        tokens = list(
            db.scalars(
                select(DevicePushToken).where(
                    DevicePushToken.motoboy_id == motoboy_id,
                    DevicePushToken.ativo.is_(True),
                )
            ).all()
        )
        if tokens:
            logger.info(
                "push_token_recuperado_outra_sub_base tipo=%s motoboy_id=%s sub_base=%s qtd=%s",
                tipo,
                motoboy_id,
                sub_base,
                len(tokens),
            )

    # Fallback 3: user_id em qualquer sub_base
    if not tokens and user_id:
        tokens = list(
            db.scalars(
                select(DevicePushToken).where(
                    DevicePushToken.user_id == user_id,
                    DevicePushToken.ativo.is_(True),
                )
            ).all()
        )
        if tokens:
            logger.info(
                "push_token_recuperado_user_qualquer_sub tipo=%s motoboy_id=%s user_id=%s qtd=%s",
                tipo,
                motoboy_id,
                user_id,
                len(tokens),
            )

    # Repara vínculo para próximos envios (mesmo tenant operacional)
    repaired = False
    for t in tokens:
        if t.motoboy_id is None or int(t.motoboy_id) != int(motoboy_id):
            t.motoboy_id = motoboy_id
            repaired = True
        if (t.sub_base or "") != sub_base:
            t.sub_base = sub_base
            repaired = True
    if repaired:
        db.flush()

    if not tokens:
        logger.info(
            "push_sem_token_ativo tipo=%s motoboy_id=%s sub_base=%s user_id=%s",
            tipo,
            motoboy_id,
            sub_base,
            user_id,
        )
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
        logger.info(
            "push_bloqueado_por_pref tipo=%s motoboy_id=%s sub_base=%s",
            tipo,
            motoboy_id,
            sub_base,
        )
        return 0

    messages = [
        _build_message(t.expo_push_token, title=title, body=body, data=data, tipo=tipo)
        for t in tokens
        if t.expo_push_token
    ]
    invalid = _send_expo(messages)
    if invalid:
        _deactivate_invalid_tokens(db, invalid)
        db.flush()

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
    chave_dedupe: Optional[str] = None,
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
    marked_users: set[int] = set()
    for t in tokens:
        if chave_dedupe and already_sent(
            db,
            destinatario_tipo="staff",
            destinatario_id=int(t.user_id),
            sub_base=sub_base,
            tipo=tipo,
            chave_dedupe=chave_dedupe,
        ):
            continue
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
        marked_users.add(int(t.user_id))

    if not messages:
        return 0

    invalid = _send_expo(messages)
    if invalid:
        _deactivate_invalid_tokens(db, invalid)
        db.flush()

    if chave_dedupe:
        for uid in marked_users:
            mark_sent(
                db,
                destinatario_tipo="staff",
                destinatario_id=uid,
                sub_base=sub_base,
                tipo=tipo,
                chave_dedupe=chave_dedupe,
            )
    return len(messages)


def enqueue_pacotes_atribuidos(
    db: Session,
    *,
    motoboy_id: int,
    sub_base: str,
    codigo: Optional[str] = None,
    delay_seconds: int = 60,
) -> None:
    """Acumula atribuições no digest (upsert atômico — seguro em lote/paralelo)."""
    now = _now()
    flush_after = now + timedelta(seconds=delay_seconds)
    # ON CONFLICT evita UniqueViolation quando N PATCH em lote batem no mesmo digest.
    stmt = (
        pg_insert(PushDigest)
        .values(
            motoboy_id=motoboy_id,
            sub_base=sub_base,
            tipo="pacotes_atribuidos",
            count=1,
            last_codigo=codigo,
            flush_after=flush_after,
            criado_em=now,
            atualizado_em=now,
        )
        .on_conflict_do_update(
            constraint="uq_push_digest_motoboy_sub_tipo",
            set_={
                "count": PushDigest.__table__.c.count + 1,
                "last_codigo": codigo if codigo else PushDigest.__table__.c.last_codigo,
                "flush_after": flush_after,
                "atualizado_em": now,
            },
        )
    )
    db.execute(stmt)


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
