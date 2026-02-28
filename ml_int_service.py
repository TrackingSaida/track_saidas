# ml_int_service.py - Serviço ML Int (OAuth, refresh, chamadas API Mercado Livre)
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
from sqlalchemy.orm import Session

from models import MLConexao

# -------------------------------------------------------------------
# URLs API Mercado Livre
# -------------------------------------------------------------------
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_ME_URL = "https://api.mercadolibre.com/users/me"
ML_ORDERS_SEARCH_URL = "https://api.mercadolibre.com/orders/search"
ML_ORDER_URL = "https://api.mercadolibre.com/orders"
ML_SHIPMENTS_SEARCH_URL = "https://api.mercadolibre.com/shipments/search"
ML_MARKETPLACE_SHIPMENT_URL = "https://api.mercadolibre.com/marketplace/shipments"


def _get_config() -> tuple[str, str, str]:
    client_id = os.getenv("ML_CLIENT_ID")
    client_secret = os.getenv("ML_CLIENT_SECRET")
    redirect_uri = os.getenv("ML_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI devem estar configurados.")
    return client_id, client_secret, redirect_uri


def exchange_code_for_token(code: str, redirect_uri: str) -> dict[str, Any]:
    """Troca o authorization code por access_token e refresh_token."""
    client_id, client_secret, _ = _get_config()
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(ML_TOKEN_URL, data=data, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_me(access_token: str) -> dict[str, Any]:
    """Obtém dados do usuário autenticado (GET /users/me)."""
    r = requests.get(ML_ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    r.raise_for_status()
    return r.json()


def refresh_ml_int_token(db: Session, conexao: MLConexao) -> Optional[MLConexao]:
    """Renova access_token usando refresh_token. Atualiza a linha e retorna o modelo."""
    client_id, client_secret, _ = _get_config()
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": conexao.refresh_token,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(ML_TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        return None
    payload = resp.json()
    conexao.access_token = payload["access_token"]
    conexao.refresh_token = payload.get("refresh_token", conexao.refresh_token)
    conexao.expires_at = datetime.utcnow() + timedelta(seconds=payload.get("expires_in", 21600))
    conexao.atualizado_em = datetime.utcnow()
    db.commit()
    db.refresh(conexao)
    return conexao


def get_valid_access_token(db: Session, user_id_ml: int, sub_base: str) -> str:
    """Retorna um access_token válido para a conexão (user_id_ml + sub_base). Renova se expirado."""
    conexao = (
        db.query(MLConexao)
        .filter(MLConexao.user_id_ml == user_id_ml, MLConexao.sub_base == sub_base)
        .first()
    )
    if not conexao:
        raise LookupError(f"Conexão ML não encontrada para user_id_ml={user_id_ml} sub_base={sub_base!r}")
    if conexao.expires_at and conexao.expires_at > datetime.utcnow():
        return conexao.access_token
    conexao = refresh_ml_int_token(db, conexao)
    if not conexao:
        raise RuntimeError("Não foi possível renovar o token ML.")
    return conexao.access_token


def fetch_orders_search(
    access_token: str,
    seller_id: int,
    *,
    order_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """GET /orders/search com seller e filtros opcionais."""
    params: dict[str, Any] = {"seller": seller_id, "offset": offset, "limit": limit}
    if order_status:
        params["order.status"] = order_status
    if date_from:
        params["order.date_created.from"] = date_from
    if date_to:
        params["order.date_created.to"] = date_to
    r = requests.get(
        ML_ORDERS_SEARCH_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_shipment(access_token: str, shipment_id: int) -> dict[str, Any]:
    """GET /marketplace/shipments/{id} com header x-format-new."""
    url = f"{ML_MARKETPLACE_SHIPMENT_URL}/{shipment_id}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}", "x-format-new": "true"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_order(access_token: str, order_id: str) -> dict[str, Any]:
    """GET /orders/{id} - retorna pedido com shipping.id quando houver."""
    r = requests.get(
        f"{ML_ORDER_URL}/{order_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_shipments_by_tracking(access_token: str, tracking_number: str) -> dict[str, Any]:
    """GET /shipments/search?tracking_number=..."""
    r = requests.get(
        ML_SHIPMENTS_SEARCH_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"tracking_number": tracking_number},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def refresh_all_ml_int_tokens(db: Session) -> None:
    """Varre ml_conexoes e renova tokens expirados (para startup da API)."""
    conexoes = db.query(MLConexao).all()
    if not conexoes:
        return
    now = datetime.utcnow()
    for c in conexoes:
        if c.expires_at and c.expires_at <= now:
            refresh_ml_int_token(db, c)
