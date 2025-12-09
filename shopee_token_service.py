# shopee_token_service.py
from __future__ import annotations

import os
import time
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List

import requests
from sqlalchemy.orm import Session

from models import ShopeeToken


# ============================================================
# Helpers de configuração
# ============================================================
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
        host = "https://partner.test-stable.shopeemobile.com"
        partner_id = int(os.getenv("SHOPEE_TEST_PARTNER_ID", "0"))
        partner_key = os.getenv("SHOPEE_TEST_PARTNER_KEY", "")

    if not partner_id or not partner_key:
        raise RuntimeError("Config Shopee (partner_id / partner_key) incompleta.")

    return host, partner_id, partner_key


def _sign_api(partner_id: int, partner_key: str, path: str, timestamp: int) -> str:
    """
    Assinatura usada nas chamadas de API:
    HMAC-SHA256( partner_key, partner_id + path + timestamp )
    """
    base_string = f"{partner_id}{path}{timestamp}"
    return hmac.new(
        partner_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ============================================================
# Funções utilitárias de token – Shopee
# ============================================================
def get_latest_shopee_token(db: Session) -> Optional[ShopeeToken]:
    """
    Pega o último token salvo (mais recente) em shopee_tokens.
    Útil se você for trabalhar com 1 loja padrão.
    """
    return (
        db.query(ShopeeToken)
        .order_by(ShopeeToken.id.desc())
        .first()
    )


def get_shopee_token_by_shop(db: Session, shop_id: int) -> Optional[ShopeeToken]:
    """
    Pega o token específico de uma shop_id.
    """
    return (
        db.query(ShopeeToken)
        .filter(ShopeeToken.shop_id == shop_id)
        .order_by(ShopeeToken.id.desc())
        .first()
    )


def refresh_shopee_token(db: Session, token: ShopeeToken) -> Optional[ShopeeToken]:
    """
    Usa o refresh_token salvo para pegar um novo access_token na Shopee.
    Atualiza a linha existente.
    Retorna None se não conseguir renovar.
    """
    if not token.refresh_token:
        # sem refresh_token não há o que fazer
        return None

    host, partner_id, partner_key = _get_shopee_config()

    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    sign = _sign_api(partner_id, partner_key, path, timestamp)

    url = f"{host}{path}"

    payload = {
        "partner_id": partner_id,
        "shop_id": token.shop_id,
        "refresh_token": token.refresh_token,
        "timestamp": timestamp,
        "sign": sign,
    }

    try:
        resp = requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"[Shopee] Erro ao conectar para refresh: {e}")
        return None

    if resp.status_code != 200:
        print(f"[Shopee] Falha ao renovar token (HTTP {resp.status_code}): {resp.text}")
        return None

    data = resp.json()
    if data.get("error"):
        print(f"[Shopee] Erro ao renovar token: {data}")
        return None

    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token", token.refresh_token)
    expire_in = data.get("expire_in") or data.get("expires_in")

    if not new_access:
        print(f"[Shopee] Resposta sem access_token: {data}")
        return None

    token.access_token = new_access
    token.refresh_token = new_refresh

    if expire_in:
        token.expires_at = datetime.utcnow() + timedelta(seconds=expire_in)

    db.commit()
    db.refresh(token)
    print(f"[Shopee] Token renovado para shop_id={token.shop_id}")
    return token


def get_valid_shopee_access_token(
    db: Session,
    shop_id: Optional[int] = None,
) -> str:
    """
    Retorna SEMPRE um access_token válido da Shopee.

    - Se ainda não expirou: devolve.
    - Se expirou: tenta refresh, salva e devolve.
    - Se não conseguir renovar: levanta exceção.

    Se shop_id for informado, usa esse.
    Se não, pega o último token da tabela.
    """
    if shop_id is not None:
        token = get_shopee_token_by_shop(db, shop_id)
    else:
        token = get_latest_shopee_token(db)

    if not token:
        raise RuntimeError("Nenhum token da Shopee foi encontrado no banco.")

    now = datetime.utcnow()
    if token.expires_at and token.expires_at > now:
        return token.access_token

    # expirado ou sem expires_at -> tenta renovar
    refreshed = refresh_shopee_token(db, token)
    if not refreshed:
        raise RuntimeError("Não foi possível renovar o token da Shopee.")

    return refreshed.access_token


def refresh_all_shopee_tokens(db: Session) -> None:
    """
    Varre TODAS as linhas de shopee_tokens.
    - Se o token estiver válido, não faz nada.
    - Se estiver vencido, tenta renovar.
    """
    tokens: List[ShopeeToken] = (
        db.query(ShopeeToken)
        .order_by(ShopeeToken.id.desc())
        .all()
    )

    if not tokens:
        print("[Shopee] Nenhum token encontrado para renovar.")
        return

    now = datetime.utcnow()
    print(f"[Shopee] Iniciando varredura de {len(tokens)} tokens...")

    for tk in tokens:
        if tk.expires_at and tk.expires_at > now:
            continue
        refreshed = refresh_shopee_token(db, tk)
        if not refreshed:
            print(f"[Shopee] Falha ao renovar token da shop_id={tk.shop_id}")
            continue

    print("[Shopee] Varredura concluída.")
