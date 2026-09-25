"""Volume do dia (Coletados ∪ Entrada) e saídas alinhadas aos Indicadores."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Set, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from acompanhamento_entradas_pure import volume_coletados_ou_entrada
from models import Coleta, Saida, SaidaHistorico

# Mesma lista de dashboard_routes.STATUS_SAIDAS_VALIDOS (modo saiu/operacional).
STATUS_SAIDAS_INDICADOR = (
    "saiu",
    "saiu pra entrega",
    "saiu_pra_entrega",
    "saiu_para_entrega",
    "em_rota",
    "entregue",
    "ausente",
)


def _bounds_dia(inicio: date, fim: date) -> Tuple[datetime, datetime]:
    return datetime.combine(inicio, time.min), datetime.combine(fim, time(23, 59, 59))


def ids_entrada_base_periodo(db: Session, sub_base: str, inicio: date, fim: date) -> Set[int]:
    dt_start, dt_end = _bounds_dia(inicio, fim)
    rows = db.execute(
        select(SaidaHistorico.id_saida)
        .join(Saida, Saida.id_saida == SaidaHistorico.id_saida)
        .where(Saida.sub_base == sub_base)
        .where(SaidaHistorico.evento == "entrada_base")
        .where(SaidaHistorico.timestamp >= dt_start)
        .where(SaidaHistorico.timestamp <= dt_end)
    ).all()
    return {int(sid) for (sid,) in rows if sid is not None}


def coletas_periodo(
    db: Session, sub_base: str, inicio: date, fim: date
) -> Tuple[int, Set[int]]:
    """Retorna (total_coletas agregado, ids_saida vinculados às coletas do período)."""
    dt_start, dt_end = _bounds_dia(inicio, fim)
    rows = db.scalars(
        select(Coleta)
        .where(Coleta.sub_base == sub_base)
        .where(Coleta.timestamp >= dt_start)
        .where(Coleta.timestamp <= dt_end)
        .where(
            (Coleta.shopee != 0)
            | (Coleta.mercado_livre != 0)
            | (Coleta.avulso != 0)
            | (Coleta.valor_total != 0)
        )
    ).all()
    total_coletas = sum((c.shopee or 0) + (c.mercado_livre or 0) + (c.avulso or 0) for c in rows)
    ids_coleta = {int(c.id_coleta) for c in rows if c.id_coleta is not None}
    ids_pacotes: Set[int] = set()
    if ids_coleta:
        ids_pacotes = {
            int(sid)
            for sid in db.scalars(
                select(Saida.id_saida).where(
                    Saida.sub_base == sub_base,
                    Saida.id_coleta.in_(ids_coleta),
                )
            ).all()
            if sid is not None
        }
    return total_coletas, ids_pacotes


def calcular_volume_que_entrou(
    db: Session, sub_base: str, inicio: date, fim: date
) -> Tuple[int, int, int]:
    """
    (volume_que_entrou, total_coletas, total_entradas).

    volume = Coletas ∪ Entradas (mesma regra do Acompanhamento / PRD-003).
    """
    ids_entrada = ids_entrada_base_periodo(db, sub_base, inicio, fim)
    total_coletas, ids_coleta_pacotes = coletas_periodo(db, sub_base, inicio, fim)
    volume = volume_coletados_ou_entrada(
        total_coletas=total_coletas,
        ids_entrada=ids_entrada,
        ids_coleta_pacotes=ids_coleta_pacotes,
    )
    return volume, total_coletas, len(ids_entrada)


def contar_saidas_como_indicadores(
    db: Session,
    sub_base: str,
    inicio: date,
    fim: date,
    *,
    modo_entregas: str = "saiu",
) -> int:
    """
    Contagem de saídas alinhada a GET /dashboard/saidas (modo saiu/operacional).

    Não exige motoboy_id — difere do KPI Pedidos do Acompanhamento (só frota).
    """
    modo = (modo_entregas or "saiu").strip().lower()
    if modo not in ("saiu", "operacional", "entregue"):
        modo = "saiu"

    rows = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            Saida.data >= inicio,
            Saida.data <= fim,
            Saida.codigo.isnot(None),
        )
    ).all()

    def _conta(status_raw: object) -> bool:
        st = (status_raw or "").strip().lower() if isinstance(status_raw, str) or status_raw is None else str(status_raw).strip().lower()
        if modo == "entregue":
            return st == "entregue"
        return st in STATUS_SAIDAS_INDICADOR

    return sum(1 for s in rows if _conta(getattr(s, "status", None)))


def volume_por_marketplace_aproximado(
    coletas_qty: int, entradas_qty: int
) -> int:
    """Sem IDs por marketplace: se um lado é 0, o outro; senão soma (teto)."""
    c = max(0, int(coletas_qty or 0))
    e = max(0, int(entradas_qty or 0))
    if c == 0:
        return e
    if e == 0:
        return c
    return c + e
