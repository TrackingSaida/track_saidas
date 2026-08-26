"""Regras puras de reversão de pedido cancelado — sem FastAPI/SQLAlchemy."""
from __future__ import annotations

from typing import Any, Iterable, Optional

ROOT_ADMIN_ROLES = {0, 1}
STATUS_CANCELADO = "CANCELADO"


def is_root_admin(role: Any) -> bool:
    try:
        return int(role) in ROOT_ADMIN_ROLES
    except (TypeError, ValueError):
        return False


def pode_alterar_pedido_cancelado(role: Any) -> bool:
    """Root (0) e admin (1) podem reverter/alterar pedido cancelado."""
    return is_root_admin(role)


def aplicar_flag_cancelado_cobranca(
    itens: Iterable[Any],
    status_anterior: str,
    status_novo: str,
) -> None:
    """Marca/desmarca cobrança ao cancelar ou reabrir pedido cancelado.

    Itens já fechados não são reabertos: o período financeiro permanece intacto.
    """
    if status_anterior == status_novo:
        return
    if status_novo != STATUS_CANCELADO and status_anterior != STATUS_CANCELADO:
        return
    if status_novo == STATUS_CANCELADO:
        for item in itens:
            item.cancelado = True
        return
    for item in itens:
        if not bool(getattr(item, "fechado", False)):
            item.cancelado = False


def mensagem_bloqueio_cancelado() -> str:
    return (
        "Pedido cancelado não pode ser alterado. "
        "Apenas root ou admin podem reverter o cancelamento."
    )


def status_parece_cancelado(raw: Optional[str]) -> bool:
    return "cancel" in (raw or "").strip().lower()


def resolver_status_antes_do_cancelamento(eventos: Iterable[Any]) -> Optional[str]:
    """Devolve o status imediatamente anterior ao último cancelamento.

    `eventos` deve estar em ordem cronológica (mais antigo primeiro).
    Cada item pode ser dict ou objeto com evento/status_anterior/status_novo.
    """
    lista = list(eventos)
    for ev in reversed(lista):
        if isinstance(ev, dict):
            evento = str(ev.get("evento") or "").strip().lower()
            status_novo = ev.get("status_novo")
            status_anterior = ev.get("status_anterior")
        else:
            evento = str(getattr(ev, "evento", None) or "").strip().lower()
            status_novo = getattr(ev, "status_novo", None)
            status_anterior = getattr(ev, "status_anterior", None)
        if evento != "cancelado" and not status_parece_cancelado(str(status_novo or "")):
            continue
        anterior = str(status_anterior or "").strip()
        if anterior and not status_parece_cancelado(anterior):
            return anterior
    return None
