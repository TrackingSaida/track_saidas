"""Regras puras da conferência de saída (sem SQLAlchemy)."""
from __future__ import annotations

from typing import Iterable, List, Optional, TypeVar

# Pacotes que deixaram de ser saída válida do motoboy. Entregue continua
# contando: conferência mede o que saiu no dia, não o que ainda está pendente.
STATUS_EXCLUIDOS_CONFERENCIA = {
    "cancelado",
    "cancelada",
    "cancelados",
    "encerrado",
    "encerrado_sistema",
    "encerrado_pelo_sistema",
}

T = TypeVar("T")


def status_conta_na_conferencia(status: Optional[str]) -> bool:
    """True se o pacote deve entrar nos totais de conferência/leitura do dia."""
    key = (status or "").strip().lower().replace(" ", "_")
    return key not in STATUS_EXCLUIDOS_CONFERENCIA


def filtrar_status_conferencia(rows: Iterable[T]) -> List[T]:
    return [s for s in rows if status_conta_na_conferencia(getattr(s, "status", None))]
