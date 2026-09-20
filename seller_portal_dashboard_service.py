"""KPIs do portal do seller (tenant-safe, escopo filtro_pedidos_seller)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from models import EnvioProprio, Saida
from seller_portal_pedidos_service import (
    _item_resumo,
    _load_details_map,
    _load_envios_map,
    _nome_base_seller,
    canal_venda_chave,
    filtro_pedidos_seller,
)


def _parse_periodo(
    periodo: Optional[str],
    de: Optional[str],
    ate: Optional[str],
) -> Tuple[datetime, datetime, str]:
    hoje = date.today()
    chave = (periodo or "hoje").strip().lower()
    if chave in ("hoje", "today"):
        ini, fim = hoje, hoje
        label = "hoje"
    elif chave in ("7d", "7", "ultimos_7"):
        ini, fim = hoje - timedelta(days=6), hoje
        label = "7d"
    elif chave in ("30d", "30", "ultimos_30"):
        ini, fim = hoje - timedelta(days=29), hoje
        label = "30d"
    elif chave in ("custom", "personalizado"):
        try:
            ini = date.fromisoformat((de or "").strip()[:10])
            fim = date.fromisoformat((ate or "").strip()[:10])
        except ValueError:
            ini, fim = hoje, hoje
        if fim < ini:
            ini, fim = fim, ini
        # limita janela a 90 dias
        if (fim - ini).days > 89:
            ini = fim - timedelta(days=89)
        label = "custom"
    else:
        ini, fim = hoje, hoje
        label = "hoje"
    start = datetime.combine(ini, datetime.min.time())
    end_exclusive = datetime.combine(fim + timedelta(days=1), datetime.min.time())
    return start, end_exclusive, label


def _norm_status(st: Optional[str]) -> str:
    return (st or "").strip().upper().replace("-", "_").replace(" ", "_")


def _bucket_status(st: Optional[str]) -> str:
    s = _norm_status(st)
    if s == "ETIQUETADO" or not s:
        return "aguardando_coleta"
    if s == "CANCELADO":
        return "cancelado"
    if s == "AUSENTE":
        return "ausente"
    if s == "ENTREGUE":
        return "entregue"
    if s in ("DEVOLVIDO", "DEVOLUCAO"):
        return "devolvido"
    if s in ("EM_ROTA", "SAIU_PARA_ENTREGA", "SAIU_PRA_ENTREGA") or "SAIU_PARA" in s:
        return "em_rota"
    if s in ("COLETADO", "SAIU"):
        return "coletado"
    return "coletado"


def dashboard_seller(
    db: Session,
    seller,
    *,
    periodo: Optional[str] = "hoje",
    de: Optional[str] = None,
    ate: Optional[str] = None,
) -> Dict[str, Any]:
    start, end_excl, periodo_label = _parse_periodo(periodo, de, ate)
    nome_base = _nome_base_seller(db, seller)
    scope = filtro_pedidos_seller(seller, nome_base)
    janela = and_(Saida.timestamp >= start, Saida.timestamp < end_excl)

    rows = list(
        db.scalars(
            select(Saida)
            .outerjoin(EnvioProprio, EnvioProprio.id_saida == Saida.id_saida)
            .where(scope, janela)
            .order_by(Saida.timestamp.desc(), Saida.id_saida.desc())
        ).all()
    )

    kpis = {
        "recebidos": 0,
        "aguardando_coleta": 0,
        "em_rota": 0,
        "coletado": 0,
        "entregues": 0,
        "cancelados": 0,
        "ausentes": 0,
        "atencao": 0,
        "taxa_sucesso": None,
    }
    por_canal = {"site": 0, "mercado_livre": 0, "shopee": 0}
    por_dia: Dict[str, int] = {}

    for s in rows:
        kpis["recebidos"] += 1
        bucket = _bucket_status(s.status)
        if bucket == "aguardando_coleta":
            kpis["aguardando_coleta"] += 1
        elif bucket == "em_rota":
            kpis["em_rota"] += 1
        elif bucket == "coletado":
            kpis["coletado"] += 1
        elif bucket == "entregue":
            kpis["entregues"] += 1
        elif bucket == "cancelado":
            kpis["cancelados"] += 1
        elif bucket == "ausente":
            kpis["ausentes"] += 1
            kpis["atencao"] += 1
        ch = canal_venda_chave(s.servico)
        if ch in por_canal:
            por_canal[ch] += 1
        dia = (s.timestamp.date().isoformat() if s.timestamp else start.date().isoformat())
        por_dia[dia] = por_dia.get(dia, 0) + 1

    finais = kpis["entregues"] + kpis["ausentes"] + kpis["cancelados"]
    if finais > 0:
        kpis["taxa_sucesso"] = round(100.0 * kpis["entregues"] / finais, 1)

    movimento = [{"data": d, "recebidos": por_dia[d]} for d in sorted(por_dia.keys())]

    recentes_src = rows[:8]
    ids = [int(r.id_saida) for r in recentes_src]
    envios = _load_envios_map(db, ids)
    details = _load_details_map(db, ids)
    recentes = [
        _item_resumo(s, envios.get(int(s.id_saida)), details.get(int(s.id_saida)))
        for s in recentes_src
    ]

    return {
        "periodo": periodo_label,
        "de": start.date().isoformat(),
        "ate": (end_excl.date() - timedelta(days=1)).isoformat(),
        "atualizado_em": datetime.utcnow().isoformat() + "Z",
        "kpis": kpis,
        "por_canal": [
            {"canal": "site", "canal_label": "Site", "total": por_canal["site"]},
            {"canal": "mercado_livre", "canal_label": "Mercado Livre", "total": por_canal["mercado_livre"]},
            {"canal": "shopee", "canal_label": "Shopee", "total": por_canal["shopee"]},
        ],
        "movimento": movimento,
        "recentes": recentes,
        "atencao": {
            "total": kpis["atencao"],
            "filtro_status": "ausente",
            "mensagem": (
                f"{kpis['atencao']} pedido(s) precisam da sua atenção"
                if kpis["atencao"]
                else "Nenhum pedido precisa de atenção agora"
            ),
        },
    }
