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
# Helpers de configuração e assinatura (replicados do shopee_routes.py para consistência)
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
        host = "https://openplatform.sandbox.test-stable.shopee.sg"
        partner_id = int(os.getenv("SHOPEE_TEST_PARTNER_ID", "0"))
        partner_key = os.getenv("SHOPEE_TEST_PARTNER_KEY", "")

    if not partner_id or not partner_key:
        raise RuntimeError("Config Shopee (partner_id / partner_key) incompleta.")

    return host, partner_id, partner_key


def _build_sign_base(
    partner_id: int,
    path: str,
    timestamp: int,
    shop_id: Optional[int] = None,
    access_token: Optional[str] = None,
) -> str:
    """
    Monta a base do sign para chamadas de API v2.
    Base: partner_id + path + timestamp + access_token (opcional) + shop_id (opcional)
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
    """
    Assinatura usada nas chamadas de API v2 (HMAC-SHA256 com partner_key).
    """
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

    # CORREÇÃO: O endpoint correto para refresh é /api/v2/auth/access_token/get
    # e a base string deve incluir o access_token antigo (que está sendo renovado)
    # e o shop_id.
    path = "/api/v2/auth/access_token/get"
    timestamp = int(time.time())
    
    # Para refresh, a Shopee exige que o access_token antigo seja incluído na base string
    # e o shop_id.
    sign = _sign_api(
        partner_id=partner_id,
        partner_key=partner_key,
        path=path,
        timestamp=timestamp,
        shop_id=token.shop_id,
        access_token=token.access_token, # Inclui o access_token antigo na assinatura
    )

    # A chamada POST para refresh não precisa de sign na URL, apenas no payload.
    # No entanto, a documentação da Shopee v2.0 é confusa.
    # Vamos manter a URL simples e passar o sign no payload, como é comum em APIs.
    # A chamada de refresh é uma exceção, onde o sign é passado no payload.
    # No entanto, a chamada para /api/v2/auth/access_token/get (que o seu código usa)
    # é a mesma chamada para obter o token pela primeira vez, mas com payload diferente.
    # A documentação v2.0 sugere que o endpoint de refresh é v2.public.refresh_access_token
    # que é um POST para /api/v2/auth/refresh_access_token.
    
    # Vamos corrigir o PATH para o endpoint de refresh correto (se o seu código estiver usando o v2.0)
    # O endpoint de refresh é /api/v2/auth/refresh_access_token
    path = "/api/v2/auth/refresh_access_token"
    
    # Recalcula o sign para o novo path.
    sign = _sign_api(
        partner_id=partner_id,
        partner_key=partner_key,
        path=path,
        timestamp=timestamp,
        shop_id=token.shop_id,
        access_token=token.access_token,
    )
    
    # A URL para o refresh é simples, sem query params de sign.
    url = f"{host}{path}"

    payload = {
        "partner_id": partner_id,
        "shop_id": token.shop_id,
        "refresh_token": token.refresh_token,
        "timestamp": timestamp,
        "sign": sign, # O sign é passado no payload para o refresh
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
