"""Vínculo automático Seller no callback OAuth (usa state assinado)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from base_papel_pure import eh_base_parceira
from marketplace_oauth_state_pure import decode_oauth_state, encode_oauth_state
from models import BasePreco, MLConexao, ShopeeToken

__all__ = [
    "decode_oauth_state",
    "encode_oauth_state",
    "validar_seller_para_vinculo",
    "tentar_vincular_ml_no_callback",
    "tentar_vincular_shopee_no_callback",
]


def validar_seller_para_vinculo(
    db: Session, sub_base: str, id_base: int
) -> Tuple[Optional[BasePreco], Optional[str]]:
    """None, mensagem_erro ou (BasePreco, None)."""
    sb = (sub_base or "").strip()
    if not sb:
        return None, "sub_base ausente no state."
    base = db.scalar(
        select(BasePreco).where(BasePreco.id_base == int(id_base), BasePreco.sub_base == sb)
    )
    if base is None:
        return None, "Seller cadastrado não encontrado nesta sub_base."
    if eh_base_parceira(getattr(base, "papel", None)):
        return None, "Base parceira não pode ser vinculada à autenticação."
    return base, None


def tentar_vincular_ml_no_callback(
    db: Session, *, user_id_ml: int, sub_base: str, id_base: int
) -> dict[str, Any]:
    """Tenta gravar id_base na conexão ML. Não levanta HTTPException (callback público)."""
    base, err = validar_seller_para_vinculo(db, sub_base, id_base)
    if err or base is None:
        return {"vinculado": False, "motivo": err or "seller_invalido"}
    conflito = (
        db.query(MLConexao)
        .filter(
            MLConexao.sub_base == sub_base,
            MLConexao.id_base == int(id_base),
            MLConexao.user_id_ml != int(user_id_ml),
        )
        .order_by(MLConexao.criado_em.desc(), MLConexao.id.desc())
        .first()
    )
    if conflito is not None:
        return {
            "vinculado": False,
            "motivo": "seller_ja_vinculado",
            "base_nome": base.base,
        }
    rows = (
        db.query(MLConexao)
        .filter(MLConexao.sub_base == sub_base, MLConexao.user_id_ml == int(user_id_ml))
        .all()
    )
    if not rows:
        return {"vinculado": False, "motivo": "conexao_nao_encontrada"}
    for c in rows:
        c.id_base = int(id_base)
        c.atualizado_em = datetime.utcnow()
    db.commit()
    return {"vinculado": True, "id_base": int(id_base), "base_nome": base.base}


def tentar_vincular_shopee_no_callback(
    db: Session, *, shop_id: int, sub_base: str, id_base: int
) -> dict[str, Any]:
    base, err = validar_seller_para_vinculo(db, sub_base, id_base)
    if err or base is None:
        return {"vinculado": False, "motivo": err or "seller_invalido"}
    conflito = (
        db.query(ShopeeToken)
        .filter(
            ShopeeToken.sub_base == sub_base,
            ShopeeToken.id_base == int(id_base),
            ShopeeToken.shop_id != int(shop_id),
        )
        .order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc())
        .first()
    )
    if conflito is not None:
        return {
            "vinculado": False,
            "motivo": "seller_ja_vinculado",
            "base_nome": base.base,
        }
    tokens = (
        db.query(ShopeeToken)
        .filter(ShopeeToken.sub_base == sub_base, ShopeeToken.shop_id == int(shop_id))
        .all()
    )
    if not tokens:
        return {"vinculado": False, "motivo": "conexao_nao_encontrada"}
    for tk in tokens:
        tk.id_base = int(id_base)
    db.commit()
    return {"vinculado": True, "id_base": int(id_base), "base_nome": base.base}
