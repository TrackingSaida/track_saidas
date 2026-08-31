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


STATUS_CONFERIDA = "conferida"


def montar_dias_conferencia_periodo(
    registros: Iterable[dict],
    datas_operacionais: Iterable[str],
) -> List[dict]:
    """Junta dias operacionais e registros de conferência.

    Dia com status conferida → Conferido.
    Qualquer outro (pendente, reconferir ou sem registro) → Não conferido.
    """
    by_date = {str(r.get("data") or ""): r for r in registros if r.get("data")}
    all_dates = sorted({str(d) for d in datas_operacionais if d} | {k for k in by_date if k})
    out: List[dict] = []
    for dia in all_dates:
        row = by_date.get(dia) or {}
        conferido = str(row.get("status") or "").strip().lower() == STATUS_CONFERIDA
        out.append(
            {
                "data": dia,
                "conferido": conferido,
                "label": "Conferido" if conferido else "Não conferido",
            }
        )
    return out
