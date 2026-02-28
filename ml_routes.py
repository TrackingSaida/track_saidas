# ml_routes.py
from __future__ import annotations

import os
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from models import MercadoLivreToken, User

router = APIRouter(prefix="/ml", tags=["Mercado Livre"])

# -------------------------------------------------------------------
# Configurações via env
# -------------------------------------------------------------------
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI")
FRONTEND_AFTER_CALLBACK = os.getenv("ML_AFTER_CALLBACK", "https://tracking-saidas.com.br/")

ML_AUTH_BASE = "https://auth.mercadolivre.com.br/authorization"
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_ME_URL = "https://api.mercadolibre.com/users/me"
ML_ORDERS_SEARCH_URL = "https://api.mercadolibre.com/orders/search"


# ============================================================
# 1) Gera o link de autorização (sem enviar dados internos à plataforma)
# ============================================================
@router.get("/connect")
def ml_connect():
    if not ML_CLIENT_ID or not ML_REDIRECT_URI:
        raise HTTPException(500, "ML_CLIENT_ID ou ML_REDIRECT_URI não configurados.")
    return {
        "auth_url": f"{ML_AUTH_BASE}?response_type=code&client_id={ML_CLIENT_ID}&redirect_uri={ML_REDIRECT_URI}"
    }


# ============================================================
# 2) Callback: troca o code por token e salva (sub_base não enviado à plataforma)
# ============================================================
@router.get("/callback")
def ml_callback(code: str, db: Session = Depends(get_db)):
    if not ML_CLIENT_ID or not ML_CLIENT_SECRET or not ML_REDIRECT_URI:
        raise HTTPException(500, "Variáveis do Mercado Livre não configuradas.")

    data = {
        "grant_type": "authorization_code",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "code": code,
        "redirect_uri": ML_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(ML_TOKEN_URL, data=data, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Erro ao obter token: {resp.text}")

    token_data = resp.json()
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)

    me_resp = requests.get(ML_ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    if me_resp.status_code != 200:
        raise HTTPException(500, "Token obtido, mas não foi possível ler /users/me")
    user_id_ml = me_resp.json()["id"]

    existente = (
        db.query(MercadoLivreToken)
        .filter(MercadoLivreToken.user_id_ml == user_id_ml)
        .first()
    )
    base_url = (FRONTEND_AFTER_CALLBACK or "").rstrip("/")
    success_page = f"{base_url}/autenticacao-sucesso.html"

    if existente:
        return RedirectResponse(url=f"{success_page}?ml=ja_existe")

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    novo = MercadoLivreToken(
        user_id_ml=user_id_ml,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    db.add(novo)
    db.commit()

    return RedirectResponse(url=f"{success_page}?ml=ok")


# ============================================================
# Listagem de sellers (tokens) filtrada por sub_base do usuário
# ============================================================
@router.get("/sellers")
def ml_sellers(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a root e admin.")
    sub_base = getattr(user, "sub_base", None)
    q = db.query(MercadoLivreToken).filter(MercadoLivreToken.sub_base == sub_base)
    tokens = q.order_by(MercadoLivreToken.criado_em.desc()).all()
    now = datetime.utcnow()
    result = []
    for tk in tokens:
        status = "conectado" if (tk.expires_at and tk.expires_at > now) else "expirado"
        result.append({
            "id": tk.id,
            "user_id_ml": tk.user_id_ml,
            "sub_base": tk.sub_base,
            "platform": "mercado_livre",
            "status": status,
            "criado_em": tk.criado_em.isoformat() if tk.criado_em else None,
        })
    return result


# ============================================================
# Helper: busca token pelo user_id_ml informado
# ============================================================
def get_token_by_user(db: Session, user_id_ml: int) -> str:
    tk = (
        db.query(MercadoLivreToken)
        .filter(MercadoLivreToken.user_id_ml == user_id_ml)
        .first()
    )
    if not tk:
        raise HTTPException(404, f"Token não encontrado para user_id_ml={user_id_ml}")
    return tk.access_token


# ============================================================
# 3) Orders como SELLER (por user_id_ml)
# ============================================================
@router.get("/orders-by-seller")
def ml_orders_by_seller(
    user_id_ml: int = Query(..., description="ID do vendedor"),
    from_date: str = Query(None, description="Data inicial ISO8601"),
    to_date: str = Query(None, description="Data final ISO8601"),
    db: Session = Depends(get_db),
):
    access_token = get_token_by_user(db, user_id_ml)
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"seller": user_id_ml, "offset": 0, "limit": 50}
    if from_date:
        params["order.date_created.from"] = from_date
    if to_date:
        params["order.date_created.to"] = to_date

    resp = requests.get(ML_ORDERS_SEARCH_URL, headers=headers, params=params)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=resp.json())
    return resp.json()


# ============================================================
# 4) Orders como BUYER (por user_id_ml)
# ============================================================
@router.get("/orders-by-buyer")
def ml_orders_by_buyer(
    user_id_ml: int = Query(..., description="ID do comprador (igual ao seu /users/me.id)"),
    from_date: str = Query(None, description="Data inicial ISO8601"),
    to_date: str = Query(None, description="Data final ISO8601"),
    db: Session = Depends(get_db),
):
    access_token = get_token_by_user(db, user_id_ml)
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"buyer": user_id_ml, "offset": 0, "limit": 50}
    if from_date:
        params["order.date_created.from"] = from_date
    if to_date:
        params["order.date_created.to"] = to_date

    resp = requests.get(ML_ORDERS_SEARCH_URL, headers=headers, params=params)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=resp.json())
    return resp.json()


# ============================================================
# 5) Consulta envio por tracking (também por user_id_ml)
# ============================================================
@router.get("/shipment-by-tracking")
def ml_shipment_by_tracking(
    user_id_ml: int = Query(..., description="ID do usuário dono do token"),
    tracking_number: str = Query(..., description="Código de rastreio"),
    db: Session = Depends(get_db),
):
    access_token = get_token_by_user(db, user_id_ml)
    headers = {"Authorization": f"Bearer {access_token}"}
    url = "https://api.mercadolibre.com/shipments/search"
    resp = requests.get(url, headers=headers, params={"tracking_number": tracking_number})
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, detail=resp.json())

    data = resp.json()
    results = data.get("results") or []
    if not results:
        raise HTTPException(404, f"Nenhum envio encontrado para '{tracking_number}'")

    shipment = results[0]
    return {
        "tracking_number": tracking_number,
        "shipment_id": shipment.get("id"),
        "receiver_address": shipment.get("receiver_address"),
        "status": shipment.get("status"),
        "substatus": shipment.get("substatus"),
    }
# ============================================================
# 6) Consulta detalhes de envio por SHIPPING_ID
# ============================================================
@router.get("/shipment-details")
def ml_shipment_details(
    user_id_ml: int = Query(..., description="ID do usuário dono do token"),
    shipping_id: int = Query(..., description="ID do envio (shipping_id)"),
    db: Session = Depends(get_db),
):
    """
    Consulta os DETALHES COMPLETOS de um envio específico no Mercado Livre.

    Endpoint oficial:
        GET https://api.mercadolibre.com/marketplace/shipments/{SHIPMENT_ID}
    Headers:
        Authorization: Bearer {ACCESS_TOKEN}
        x-format-new: true

    Retorna: status, substatus, tracking, origem, destino, endereço, etc.
    """

    # Busca o token correspondente a esse user_id_ml
    tk = (
        db.query(MercadoLivreToken)
        .filter(MercadoLivreToken.user_id_ml == user_id_ml)
        .first()
    )
    if not tk:
        raise HTTPException(404, f"Token não encontrado para user_id_ml={user_id_ml}")

    headers = {
        "Authorization": f"Bearer {tk.access_token}",
        "x-format-new": "true",
    }

    url = f"https://api.mercadolibre.com/marketplace/shipments/{shipping_id}"
    resp = requests.get(url, headers=headers)

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = {"raw": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=detail)

    data = resp.json()

    # Retorna os dados principais (ou tudo, se preferir)
    return {
        "shipping_id": shipping_id,
        "status": data.get("status"),
        "substatus": data.get("substatus"),
        "tracking_number": data.get("tracking_number"),
        "origin": data.get("origin"),
        "destination": data.get("destination"),
        "dimensions": data.get("dimensions"),
        "lead_time": data.get("lead_time"),
        "raw": data,  # mantém o JSON completo para debug
    }
