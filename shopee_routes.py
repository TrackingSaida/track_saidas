# shopee_routes.py
from __future__ import annotations

import os
import time
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse


from db import get_db
from models import ShopeeToken

router = APIRouter(prefix="/shopee", tags=["Shopee"])


# -------------------------------------------------
# Helpers de configuração
# -------------------------------------------------
def _get_shopee_config():
    """
    Lê as configs da Shopee do .env
    Usa SHOPEE_ENV para decidir entre sandbox x produção.
    """
    env = os.getenv("SHOPEE_ENV", "sandbox").lower()

    if env == "production":
        host = "https://partner.shopeemobile.com"
        partner_id = int(os.getenv("SHOPEE_PROD_PARTNER_ID", "0"))
        partner_key = os.getenv("SHOPEE_PROD_PARTNER_KEY", "")
    else:
        # SANDBOX
        # (você confirmou que este host é o correto no seu ambiente)
        host = "https://openplatform.sandbox.test-stable.shopee.sg"
        partner_id = int(os.getenv("SHOPEE_TEST_PARTNER_ID", "0"))
        partner_key = os.getenv("SHOPEE_TEST_PARTNER_KEY", "")

    redirect_url = os.getenv("SHOPEE_REDIRECT_URL")

    if not partner_id or not partner_key or not redirect_url:
        raise RuntimeError("Config Shopee incompleta nas variáveis de ambiente.")

    return host, partner_id, partner_key, redirect_url, env


def _build_sign_base(
    partner_id: int,
    path: str,
    timestamp: int,
    shop_id: Optional[int] = None,
    access_token: Optional[str] = None,
) -> str:
    """
    Base string usada para assinatura HMAC-SHA256.

    Observação prática:
    - Para auth_partner: base = partner_id + path + timestamp
    - Para token/get:    base = partner_id + path + timestamp   (NÃO inclui shop_id)
    - Para chamadas com access_token (ex.: pedidos): geralmente inclui access_token + shop_id
    """
    parts: list[str] = [str(partner_id), path, str(timestamp)]
    if access_token:
        parts.append(access_token)
    if shop_id is not None:
        parts.append(str(shop_id))
    return "".join(parts)


def _sign_api(
    partner_id: int,
    partner_key: str,
    path: str,
    timestamp: int,
    shop_id: Optional[int] = None,
    access_token: Optional[str] = None,
) -> str:
    base_string = _build_sign_base(
        partner_id=partner_id,
        path=path,
        timestamp=timestamp,
        shop_id=shop_id,
        access_token=access_token,
    )
    return hmac.new(
        partner_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# -------------------------------------------------
# 1) Gerar URL de autorização da Shopee
# -------------------------------------------------
@router.get(
    "/auth-url",
    summary="Gera a URL para o seller autorizar a Shopee",
)
def gerar_auth_url():
    host, partner_id, partner_key, redirect_url, env = _get_shopee_config()

    path = "/api/v2/shop/auth_partner"
    timestamp = int(time.time())

    # auth_partner: base = partner_id + path + timestamp
    sign = _sign_api(
        partner_id=partner_id,
        partner_key=partner_key,
        path=path,
        timestamp=timestamp,
        shop_id=None,
        access_token=None,
    )

    auth_url = (
        f"{host}{path}"
        f"?partner_id={partner_id}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
        f"&redirect={redirect_url}"
    )
    return {"auth_url": auth_url}


# -------------------------------------------------
# 2) Callback: troca code por token e salva em shopee_tokens
# -------------------------------------------------
@router.get(
    "/callback",
    summary="Callback da Shopee (troca code por tokens)",
)
def shopee_callback(
    code: str = Query(...),
    shop_id: int = Query(...),
    main_account_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    host, partner_id, partner_key, _, env = _get_shopee_config()

    path = "/api/v2/auth/token/get"
    timestamp = int(time.time())

    sign = _sign_api(
        partner_id=partner_id,
        partner_key=partner_key,
        path=path,
        timestamp=timestamp,
        shop_id=shop_id,
    )

    url = f"{host}{path}?partner_id={partner_id}&timestamp={timestamp}&sign={sign}"

    payload = {
        "code": code,
        "shop_id": shop_id,
        "partner_id": partner_id,
    }

    try:
        resp = requests.post(url, json=payload, timeout=20)
        data = resp.json()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao conectar na Shopee: {e}",
        )

    if resp.status_code != 200 or not data.get("access_token"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "msg": "Erro ao obter token Shopee",
                "body": data,
            },
        )

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expire_in") or data.get("expires_in")

    expires_at = None
    if expires_in:
        expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))

    existente = (
        db.query(ShopeeToken)
        .filter(ShopeeToken.shop_id == shop_id)
        .first()
    )

    if existente:
        existente.access_token = access_token
        existente.refresh_token = refresh_token or existente.refresh_token
        existente.expires_at = expires_at
        existente.main_account_id = main_account_id
        db.commit()
    else:
        token = ShopeeToken(
            shop_id=shop_id,
            main_account_id=main_account_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
        db.add(token)
        db.commit()

    # ✅ SUCESSO → REDIRECT PARA A LANDING
    return RedirectResponse(
        url="https://tracking-saidas.com.br/landing-tracking.html",
        status_code=302,
    )
