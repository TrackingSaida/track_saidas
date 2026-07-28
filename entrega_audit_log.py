"""Logs estruturados do fluxo de conclusão de entrega (foto + marcar status).

Formato estável para grep em produção:
  audit_entrega event=... id_saida=... user_id=... client_action_id=... result=...
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("entrega_audit")


def norm_client_action_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()[:64]
    return value or None


def audit_entrega(event: str, **fields: Any) -> None:
    parts = [f"audit_entrega event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        text = str(value).replace(" ", "_")
        if len(text) > 160:
            text = text[:160]
        parts.append(f"{key}={text}")
    logger.info(" ".join(parts))
