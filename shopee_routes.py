# shopee_routes.py
from __future__ import annotations

import logging
import os
import time
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from fastapi.responses import RedirectResponse

from auth import get_current_user
from db import get_db
from models import ShopeeToken, User

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


def _fetch_shop_name(
    host: str,
    partner_id: int,
    partner_key: str,
    shop_id: int,
    access_token: str,
) -> Optional[str]:
    """Chama Get Shop Info e retorna o nome da loja, ou None em caso de erro."""
    path = "/api/v2/shop/get_shop_info"
    timestamp = int(time.time())
    sign = _sign_api(
        partner_id=partner_id,
        partner_key=partner_key,
        path=path,
        timestamp=timestamp,
        shop_id=shop_id,
        access_token=access_token,
    )
    url = (
        f"{host}{path}"
        f"?partner_id={partner_id}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
        f"&shop_id={shop_id}"
        f"&access_token={quote(access_token, safe='')}"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json() if resp.text else {}
        err = data.get("error")
        msg = data.get("message", "")
        if resp.status_code != 200 or err:
            logger.warning(
                "Shopee Get Shop Info falhou: shop_id=%s status=%s error=%s message=%s",
                shop_id, resp.status_code, err, msg,
            )
            print(f"[Shopee Get Shop Info] FALHOU shop_id={shop_id} status={resp.status_code} error={err!r} message={msg!r}", flush=True)
            return None
        # Resposta: pode vir em data["response"] (objeto com shop_name/name/username) ou direto em data
        info = data.get("response")
        if isinstance(info, dict):
            name = (
                info.get("shop_name")
                or info.get("name")
                or info.get("shopname")
                or info.get("username")  # sandbox às vezes retorna só username
            )
        else:
            name = (
                data.get("shop_name")
                or data.get("name")
                or data.get("shopname")
                or data.get("username")
            )
        result = (name or "").strip() or None
        if result:
            logger.info("Shopee Get Shop Info ok: shop_id=%s shop_name=%s", shop_id, result)
            print(f"[Shopee Get Shop Info] OK shop_id={shop_id} shop_name={result!r}", flush=True)
        else:
            # Log das chaves disponíveis para ajustar extração
            keys_top = list(data.keys()) if isinstance(data, dict) else []
            keys_resp = list(info.keys()) if isinstance(info, dict) else []
            logger.info(
                "Shopee Get Shop Info sem nome: shop_id=%s keys=%s response_keys=%s",
                shop_id, keys_top, keys_resp,
            )
            print(f"[Shopee Get Shop Info] SEM NOME shop_id={shop_id} data_keys={keys_top} response_keys={keys_resp}", flush=True)
        return result
    except Exception as e:
        logger.warning("Shopee Get Shop Info exception: shop_id=%s err=%s", shop_id, e)
        print(f"[Shopee Get Shop Info] EXCEPTION shop_id={shop_id} err={e!r}", flush=True)
        return None


# -------------------------------------------------
# 1) Gerar URL de autorização da Shopee (sem enviar dados internos à plataforma)
# -------------------------------------------------
@router.get(
    "/auth-url",
    summary="Gera a URL para o seller autorizar a Shopee",
)
def gerar_auth_url(state: Optional[str] = Query(None, alias="state")):
    host, partner_id, partner_key, redirect_url, env = _get_shopee_config()

    # Se state (sub_base) vier preenchido, colocamos na URL de redirect para o callback receber
    state_val = (state or "").strip()
    if state_val:
        redirect = redirect_url + ("&" if "?" in redirect_url else "?") + "state=" + quote(state_val, safe="")
    else:
        redirect = redirect_url

    path = "/api/v2/shop/auth_partner"
    timestamp = int(time.time())

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
        f"&redirect={quote(redirect, safe='')}"
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
    state: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    host, partner_id, partner_key, _, env = _get_shopee_config()

    path = "/api/v2/auth/token/get"
    timestamp = int(time.time())

    # ✅ CORRETO: NÃO inclui shop_id na assinatura
    base_string = f"{partner_id}{path}{timestamp}"

    sign = hmac.new(
        partner_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = (
        f"{host}{path}"
        f"?partner_id={partner_id}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
    )

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

    sub_base_val = (state or "").strip() or None

    if existente:
        existente.access_token = access_token
        existente.refresh_token = refresh_token or existente.refresh_token
        existente.expires_at = expires_at
        existente.main_account_id = main_account_id
        existente.sub_base = sub_base_val
        db.commit()
        record = existente
    else:
        token = ShopeeToken(
            shop_id=shop_id,
            main_account_id=main_account_id,
            sub_base=sub_base_val,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
        db.add(token)
        db.commit()
        db.refresh(token)
        record = token

    # Buscar nome da loja (Get Shop Info) para exibir no front como no ML
    shop_name = _fetch_shop_name(host, partner_id, partner_key, shop_id, access_token)
    if shop_name:
        record.shop_name = shop_name
        db.commit()

    frontend_base = (os.getenv("ML_AFTER_CALLBACK", "https://tracking-saidas.com.br/") or "").rstrip("/")
    success_url = f"{frontend_base}/autenticacao-sucesso.html?shopee=ok"
    return RedirectResponse(url=success_url, status_code=302)


# -------------------------------------------------
# RefreshAccessToken — renovação agendada (a cada ~5h)
# -------------------------------------------------
def refresh_shopee_token(db: Session, token: ShopeeToken) -> bool:
    """
    Renova access_token e refresh_token de um ShopeeToken via API RefreshAccessToken.
    Atualiza access_token, refresh_token e expires_at no registro. Retorna True se ok, False em erro.
    """
    if not token.refresh_token:
        return False
    try:
        host, partner_id, partner_key, _, _ = _get_shopee_config()
    except RuntimeError as e:
        logger.warning("refresh_shopee_token shop_id=%s: config falhou: %s", token.shop_id, e)
        return False
    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    base_string = f"{partner_id}{path}{timestamp}"
    sign = hmac.new(
        partner_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = (
        f"{host}{path}"
        f"?partner_id={partner_id}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
    )
    payload = {
        "shop_id": token.shop_id,
        "refresh_token": token.refresh_token,
        "partner_id": partner_id,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        data = resp.json()
    except Exception as e:
        logger.warning("refresh_shopee_token shop_id=%s: request falhou: %s", token.shop_id, e)
        return False
    if resp.status_code != 200 or not data.get("access_token"):
        logger.warning(
            "refresh_shopee_token shop_id=%s: status=%s error=%s",
            token.shop_id, resp.status_code, data.get("error") or data.get("message"),
        )
        return False
    token.access_token = data["access_token"]
    token.refresh_token = data.get("refresh_token") or token.refresh_token
    expires_in = data.get("expire_in") or data.get("expires_in")
    if expires_in is not None:
        token.expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
    db.commit()
    return True


def refresh_all_shopee_tokens(db: Session) -> int:
    """Renova todos os ShopeeToken que tenham refresh_token (chamado no startup e pelo cron a cada ~5h). Retorna quantos foram renovados."""
    tokens = db.query(ShopeeToken).filter(ShopeeToken.refresh_token.isnot(None)).all()
    n = 0
    for tk in tokens:
        try:
            if refresh_shopee_token(db, tk):
                n += 1
        except Exception as e:
            logger.warning("refresh_all_shopee_tokens shop_id=%s: %s", tk.shop_id, e)
    return n


# -------------------------------------------------
# 3) Listagem de sellers (tokens) filtrada por sub_base do usuário
# -------------------------------------------------
@router.get("/sellers")
def shopee_sellers(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a root e admin.")
    sub_base = getattr(user, "sub_base", None)
    tokens = (
        db.query(ShopeeToken)
        .filter(ShopeeToken.sub_base == sub_base)
        .order_by(ShopeeToken.criado_em.desc())
        .all()
    )
    now = datetime.utcnow()
    result = []
    try:
        host, partner_id, partner_key, _, _ = _get_shopee_config()
    except RuntimeError:
        host = partner_id = partner_key = None
    for tk in tokens:
        status = "conectado" if (tk.expires_at and tk.expires_at > now) else "expirado"
        shop_name = tk.shop_name
        # Preencher nome para tokens antigos que ainda não têm (token válido)
        if not shop_name and status == "conectado" and host and partner_id and partner_key:
            shop_name = _fetch_shop_name(host, partner_id, partner_key, tk.shop_id, tk.access_token)
            if shop_name:
                tk.shop_name = shop_name
                db.commit()
        result.append({
            "id": tk.id,
            "shop_id": tk.shop_id,
            "main_account_id": tk.main_account_id,
            "sub_base": tk.sub_base,
            "platform": "shopee",
            "user_nickname_shopee": shop_name,
            "status": status,
            "criado_em": tk.criado_em.isoformat() if tk.criado_em else None,
        })
    return result