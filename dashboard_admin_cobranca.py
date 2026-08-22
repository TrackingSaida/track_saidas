"""Cálculo da base de cobrança do Indicador Admin.

Regra por pacote (id_saida), no período filtrado:

1. Teve coleta no período → cobra como coleta.
2. Senão, primeira entrada no período e nunca teve coleta → cobra como entrada.
3. Senão, saída válida no período sem coleta e sem entrada → cobra como só saída
   (avulso lançado na saída e volume anterior à virada para entrada).

O mesmo pacote nunca entra em mais de uma origem.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from models import Owner

STATUS_SAIDAS_COBRANCA = (
    "saiu",
    "saiu pra entrega",
    "saiu_pra_entrega",
    "saiu_para_entrega",
    "em_rota",
    "entregue",
    "ausente",
)
STATUS_CANCELADOS = ("cancelado", "cancelada")

IdSubBase = Tuple[int, str]


@dataclass(frozen=True)
class CobrancaAdminOwner:
    sub_base: str
    cobranca_coleta: int = 0
    cobranca_entrada: int = 0
    cobranca_saida: int = 0

    @property
    def base_cobranca(self) -> int:
        return self.cobranca_coleta + self.cobranca_entrada + self.cobranca_saida


def tipo_operacao_owner(ignorar_coleta: bool, entrada_habilitada: bool) -> str:
    if entrada_habilitada and not ignorar_coleta:
        return "Coleta + Entrada"
    if entrada_habilitada:
        return "Entrada"
    if ignorar_coleta:
        return "Só Saída"
    return "Coleta"


def detalhe_base_cobranca(n_coleta: int, n_entrada: int, n_saida: int) -> str:
    parts: List[str] = []
    if n_coleta:
        parts.append(f"{n_coleta} coleta")
    if n_entrada:
        parts.append(f"{n_entrada} entrada")
    if n_saida:
        parts.append(f"{n_saida} só saída")
    if not parts:
        return "0 pacotes"
    return " + ".join(parts)


def origem_cobranca_pacote(
    *,
    teve_coleta_no_periodo: bool,
    primeira_entrada_no_periodo: bool,
    saida_valida_no_periodo: bool,
    teve_coleta_alguma_vez: bool,
    teve_entrada_alguma_vez: bool,
) -> Optional[str]:
    """Classifica um pacote em coleta | entrada | saida. None = fora da base."""
    if teve_coleta_no_periodo:
        return "coleta"
    if primeira_entrada_no_periodo and not teve_coleta_alguma_vez:
        return "entrada"
    if saida_valida_no_periodo and not teve_coleta_alguma_vez and not teve_entrada_alguma_vez:
        return "saida"
    return None


def agregar_cobranca_por_owner(
    sub_bases: Sequence[str],
    coleta_ids: Iterable[IdSubBase],
    entrada_ids: Iterable[IdSubBase],
    saida_ids: Iterable[IdSubBase],
) -> Dict[str, CobrancaAdminOwner]:
    """Une origens sem duplicar id_saida. Prioridade: coleta > entrada > só saída."""
    counts: Dict[str, Dict[str, int]] = {
        sb: {"coleta": 0, "entrada": 0, "saida": 0} for sb in sub_bases
    }
    seen: set[int] = set()

    def _add(rows: Iterable[IdSubBase], origem: str) -> None:
        for id_saida, sub_base in rows:
            if id_saida in seen:
                continue
            seen.add(id_saida)
            bucket = counts.get(sub_base)
            if bucket is None:
                continue
            bucket[origem] += 1

    _add(coleta_ids, "coleta")
    _add(entrada_ids, "entrada")
    _add(saida_ids, "saida")
    return {
        sb: CobrancaAdminOwner(
            sub_base=sb,
            cobranca_coleta=vals["coleta"],
            cobranca_entrada=vals["entrada"],
            cobranca_saida=vals["saida"],
        )
        for sb, vals in counts.items()
    }


def receita_admin_owner(valor: Decimal, base_cobranca: int) -> Decimal:
    return (valor * base_cobranca).quantize(Decimal("0.01"))


def _status_nao_cancelado():
    from models import Saida

    return func.lower(func.coalesce(Saida.status, "")).notin_(STATUS_CANCELADOS)


def buscar_ids_coleta_periodo(
    db: Session,
    sub_bases: Sequence[str],
    dt_min: datetime,
    dt_max: datetime,
) -> List[IdSubBase]:
    from models import Coleta, Saida

    rows = db.execute(
        select(Saida.id_saida, Saida.sub_base)
        .join(Coleta, Coleta.id_coleta == Saida.id_coleta)
        .where(
            Saida.sub_base.in_(sub_bases),
            Coleta.timestamp >= dt_min,
            Coleta.timestamp <= dt_max,
            _status_nao_cancelado(),
        )
    ).all()
    return [(int(r.id_saida), r.sub_base or "") for r in rows]


def buscar_ids_entrada_periodo(
    db: Session,
    sub_bases: Sequence[str],
    start_hist: datetime,
    end_hist: datetime,
    *,
    somente_sem_coleta: bool,
) -> List[IdSubBase]:
    from models import Saida, SaidaHistorico

    primeira_entrada = (
        select(
            SaidaHistorico.id_saida.label("id_saida"),
            func.min(SaidaHistorico.timestamp).label("ts"),
        )
        .where(SaidaHistorico.evento == "entrada_base")
        .group_by(SaidaHistorico.id_saida)
        .having(
            and_(
                func.min(SaidaHistorico.timestamp) >= start_hist,
                func.min(SaidaHistorico.timestamp) < end_hist,
            )
        )
        .subquery()
    )
    stmt = (
        select(Saida.id_saida, Saida.sub_base)
        .join(primeira_entrada, primeira_entrada.c.id_saida == Saida.id_saida)
        .where(
            Saida.sub_base.in_(sub_bases),
            _status_nao_cancelado(),
        )
    )
    if somente_sem_coleta:
        stmt = stmt.where(Saida.id_coleta.is_(None))
    rows = db.execute(stmt).all()
    return [(int(r.id_saida), r.sub_base or "") for r in rows]


def buscar_ids_saida_sem_origem(
    db: Session,
    sub_bases: Sequence[str],
    data_inicio: date,
    data_fim: date,
) -> List[IdSubBase]:
    from models import Saida, SaidaHistorico

    existe_entrada = exists(
        select(SaidaHistorico.id).where(
            SaidaHistorico.id_saida == Saida.id_saida,
            SaidaHistorico.evento == "entrada_base",
        )
    )
    rows = db.execute(
        select(Saida.id_saida, Saida.sub_base).where(
            Saida.sub_base.in_(sub_bases),
            Saida.data >= data_inicio,
            Saida.data <= data_fim,
            Saida.codigo.isnot(None),
            func.lower(Saida.status).in_(STATUS_SAIDAS_COBRANCA),
            Saida.id_coleta.is_(None),
            ~existe_entrada,
        )
    ).all()
    return [(int(r.id_saida), r.sub_base or "") for r in rows]


def carregar_cobranca_admin(
    db: Session,
    sub_bases: Sequence[str],
    *,
    data_inicio: date,
    data_fim: date,
    dt_min: datetime,
    dt_max: datetime,
    start_hist: datetime,
    end_hist: datetime,
) -> Dict[str, CobrancaAdminOwner]:
    coleta_ids = buscar_ids_coleta_periodo(db, sub_bases, dt_min, dt_max)
    entrada_ids = buscar_ids_entrada_periodo(
        db, sub_bases, start_hist, end_hist, somente_sem_coleta=True
    )
    saida_ids = buscar_ids_saida_sem_origem(db, sub_bases, data_inicio, data_fim)
    return agregar_cobranca_por_owner(sub_bases, coleta_ids, entrada_ids, saida_ids)


def valor_owner(owner: Optional["Owner"]) -> Decimal:
    if owner is None:
        return Decimal("0")
    try:
        return Decimal(str(owner.valor or 0))
    except Exception:
        return Decimal("0")
