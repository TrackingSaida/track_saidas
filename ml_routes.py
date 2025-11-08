# ml_routes.py
from __future__ import annotations

import os
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import select

from db import get_db
from models import MercadoLivreToken

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
# 1) Gera o link de autorização
# ============================================================
@router.get("/connect")
def ml_connect():
    if not ML_CLIENT_ID or not ML_REDIRECT_URI:
        raise HTTPException(500, "ML_CLIENT_ID ou ML_REDIRECT_URI não configurados.")
    return {
        "auth_url": f"{ML_AUTH_BASE}?response_type=code&client_id={ML_CLIENT_ID}&redirect_uri={ML_REDIRECT_URI}"
    }


# ============================================================
# 2) Callback: troca o code por token e salva
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
    if existente:
        final_url = f"{FRONTEND_AFTER_CALLBACK}?ml=ja_existe"
        return RedirectResponse(url=final_url)

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    novo = MercadoLivreToken(
        user_id_ml=user_id_ml,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    db.add(novo)
    db.commit()

    return RedirectResponse(f"{FRONTEND_AFTER_CALLBACK}?ml=ok")


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
