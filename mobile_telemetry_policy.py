"""Regras puras da telemetria mobile (sem DB) — PRD-018."""
from __future__ import annotations

import os
from typing import Any, Optional

ALLOWED_EVENT_NAMES = frozenset(
    {
        "saida_confirmada_client",
        "pendentes_abertos",
        "baixa_form_aberto",
        "baixa_foto_capturada",
        "baixa_confirm_tap",
        "outbox_enqueued",
        "outbox_sync_stage",
        "app_foreground",
        "device_snapshot",
    }
)

PROPS_WHITELIST = frozenset(
    {
        "sessao_ids_count",
        "tab",
        "kind",
        "photo_count",
        "error_code",
        "is_device",
        "net_type",
    }
)

DEFAULT_ALLOWLIST = ("GG FLEX",)


def telemetry_sub_base_allowlist() -> set[str]:
    raw = (os.getenv("MOBILE_TELEMETRY_SUB_BASES") or "").strip()
    if not raw:
        return {s.strip() for s in DEFAULT_ALLOWLIST if s.strip()}
    return {part.strip() for part in raw.split(",") if part.strip()}


def clip_text(value: Optional[str], max_len: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


def sanitize_props(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in PROPS_WHITELIST:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                out[key] = value[:120]
            else:
                out[key] = value
        if len(out) >= 12:
            break
    return out or None


def is_allowed_event_name(name: Optional[str]) -> bool:
    return (name or "").strip() in ALLOWED_EVENT_NAMES
