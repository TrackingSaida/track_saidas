# coleta_a_coletar_service.py - Pedidos sincronizados aguardando coleta e contas conectadas por seller
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from models import MLConexao, Saida, ShopeeToken
from pedido_sincronizado import STATUS_AGUARDANDO_COLETA

CANAL_ML = "Mercado Livre"
CANAL_SHOPEE = "Shopee"


def hoje_operacao() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def chave_base(nome: Optional[str]) -> str:
    return (nome or "").strip().upper()


def contagem_a_coletar(db: Session, sub_base: str) -> dict[str, dict[str, int]]:
    """{BASE: {mercado_livre, shopee}} dos pedidos sincronizados ainda aguardando coleta na sub_base."""
    chave = func.upper(func.trim(Saida.base))
    rows = db.execute(
        select(
            chave,
            func.sum(case((Saida.ml_shipment_id.isnot(None), 1), else_=0)),
            func.sum(case((Saida.shopee_order_sn.isnot(None), 1), else_=0)),
        )
        .where(
            Saida.sub_base == sub_base,
            func.lower(func.coalesce(Saida.status, "")) == STATUS_AGUARDANDO_COLETA,
        )
        .group_by(chave)
    ).all()
    return {str(base or ""): {"mercado_livre": int(ml or 0), "shopee": int(sh or 0)} for base, ml, sh in rows}


def contas_por_base(db: Session, sub_base: str) -> dict[int, list[dict[str, str]]]:
    """{id_base: [{canal, conta}]} das contas ML/Shopee vinculadas a sellers da sub_base."""
    out: dict[int, list[dict[str, str]]] = {}
    vistos: set[tuple[str, int]] = set()
    ml = db.scalars(
        select(MLConexao)
        .where(MLConexao.sub_base == sub_base, MLConexao.id_base.isnot(None))
        .order_by(MLConexao.criado_em.desc(), MLConexao.id.desc())
    ).all()
    for c in ml:
        if (CANAL_ML, c.user_id_ml) in vistos:
            continue
        vistos.add((CANAL_ML, c.user_id_ml))
        out.setdefault(int(c.id_base), []).append({"canal": CANAL_ML, "conta": (c.user_nickname_ml or "").strip()})
    shopee = db.scalars(
        select(ShopeeToken)
        .where(ShopeeToken.sub_base == sub_base, ShopeeToken.id_base.isnot(None))
        .order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc())
    ).all()
    for t in shopee:
        if (CANAL_SHOPEE, t.shop_id) in vistos:
            continue
        vistos.add((CANAL_SHOPEE, t.shop_id))
        out.setdefault(int(t.id_base), []).append({"canal": CANAL_SHOPEE, "conta": (t.shop_name or "").strip()})
    return out


def a_coletar_item(contas: list[dict[str, str]], contagem: Optional[dict[str, int]]) -> Optional[dict[str, int]]:
    """None quando o seller não tem conta conectada nem pedido aguardando coleta."""
    ml = int((contagem or {}).get("mercado_livre") or 0)
    sh = int((contagem or {}).get("shopee") or 0)
    if not contas and not (ml or sh):
        return None
    return {"total": ml + sh, "mercado_livre": ml, "shopee": sh}


def resumo_a_coletar(itens: list[dict[str, Any]]) -> dict[str, int]:
    com = [i["a_coletar"] for i in itens if i.get("a_coletar") and i["a_coletar"]["total"] > 0]
    return {
        "total": sum(a["total"] for a in com),
        "mercado_livre": sum(a["mercado_livre"] for a in com),
        "shopee": sum(a["shopee"] for a in com),
        "sellers": len(com),
    }
