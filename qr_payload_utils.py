"""Utilitários compartilhados para qr_payload_raw (Mercado Livre / etiqueta)."""
from __future__ import annotations

import re
from typing import Any, Optional


MSG_QR_ALERTA_ML = (
    "Leitura ok. Bipe de novo o QR do Mercado Livre para salvar a etiqueta completa."
)


def is_ml_servico(servico: Optional[str]) -> bool:
    s = (servico or "").strip().lower()
    if not s:
        return False
    return "mercado" in s or "flex" in s or bool(re.search(r"\bml\b", s)) or s == "ml"


def is_ml_qr_completo(qr_raw: Optional[str]) -> bool:
    """Payload forte para etiqueta ML: JSON com sender_id e/ou hash_code."""
    if not qr_raw or not str(qr_raw).strip():
        return False
    raw = str(qr_raw).strip()
    if not raw.startswith("{"):
        return False
    return (
        "sender_id" in raw
        or "SENDER_ID" in raw
        or "hash_code" in raw
        or "HASH_CODE" in raw
    )


def should_store_qr_payload_raw(servico: Optional[str], qr_raw: Optional[str]) -> bool:
    """Armazena qr_payload_raw somente para Mercado Livre com formato válido."""
    if not qr_raw or not str(qr_raw).strip():
        return False
    if not is_ml_servico(servico):
        return False
    raw = str(qr_raw).strip()
    if is_ml_qr_completo(raw):
        return True
    # Formato antigo / código de barras ML (4[5-9]...)
    if re.search(r"4[5-9]\d{9}", raw):
        return True
    return False


def has_usable_qr_etiqueta(qr_raw: Optional[str]) -> bool:
    """True se há payload utilizável na geração de etiqueta ML."""
    if not qr_raw or not str(qr_raw).strip():
        return False
    raw = str(qr_raw).strip()
    if is_ml_qr_completo(raw):
        return True
    if re.search(r"4[5-9]\d{9}", raw):
        return True
    return False


def needs_qr_update(
    saida: Any,
    qr_novo: Optional[str],
    servico: Optional[str] = None,
) -> bool:
    """
    True quando o payload novo deve sobrescrever/completar o atual:
    - novo é armazenável; e
    - atual vazio; ou atual fraco (não JSON completo) e novo é JSON completo.
    """
    serv = servico if servico is not None else getattr(saida, "servico", None)
    if not should_store_qr_payload_raw(serv, qr_novo):
        return False
    atual = (getattr(saida, "qr_payload_raw", None) or "").strip()
    if not atual:
        return True
    if is_ml_qr_completo(atual):
        return False
    # Atual fraco (só dígitos / sem marcadores): só sobe se o novo for completo.
    return is_ml_qr_completo(qr_novo)


def apply_qr_payload_if_needed(
    saida: Any,
    qr_novo: Optional[str],
    servico: Optional[str] = None,
) -> dict:
    """
    Aplica upgrade de qr_payload_raw quando necessário.
    Retorna {"updated": bool, "alerta": bool}.
    alerta = serviço ML e, após a operação, ainda sem JSON completo da etiqueta.
    """
    serv = servico if servico is not None else getattr(saida, "servico", None)
    updated = False
    if needs_qr_update(saida, qr_novo, serv):
        saida.qr_payload_raw = str(qr_novo).strip()
        updated = True

    alerta = False
    if is_ml_servico(serv):
        atual = (getattr(saida, "qr_payload_raw", None) or "").strip()
        if not is_ml_qr_completo(atual):
            alerta = True
    return {"updated": updated, "alerta": alerta}


def qr_flags_for_response(result: dict) -> dict:
    """Campos opcionais backward-compatible para respostas de leitura."""
    out: dict = {}
    if result.get("updated"):
        out["qr_atualizado"] = True
    if result.get("alerta"):
        out["qr_alerta"] = True
        out["qr_alerta_mensagem"] = MSG_QR_ALERTA_ML
    return out
