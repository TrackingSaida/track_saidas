# shopee_routes.py
from __future__ import annotations

import logging
import os
import threading
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
from pydantic import BaseModel

from sqlalchemy import select

from auth import get_current_user
from db import get_db
from models import BasePreco, Owner, ShopeeToken, User

router = APIRouter(prefix="/shopee", tags=["Shopee"])

SHOPEE_TOKEN_REFRESH_BUFFER_SECONDS = int(os.getenv("SHOPEE_TOKEN_REFRESH_BUFFER_SECONDS", "300"))

_shopee_refresh_locks: dict[int, threading.Lock] = {}
_shopee_refresh_locks_guard = threading.Lock()


def _utcnow() -> datetime:
    return datetime.utcnow()


def _get_shopee_refresh_lock(shop_id: int) -> threading.Lock:
    with _shopee_refresh_locks_guard:
        if shop_id not in _shopee_refresh_locks:
            _shopee_refresh_locks[shop_id] = threading.Lock()
        return _shopee_refresh_locks[shop_id]


def shopee_token_precisa_renovar(token: ShopeeToken, buffer_seconds: int | None = None) -> bool:
    if not (token.refresh_token or "").strip():
        return False
    if not token.expires_at:
        return True
    buf = SHOPEE_TOKEN_REFRESH_BUFFER_SECONDS if buffer_seconds is None else buffer_seconds
    threshold = _utcnow() + timedelta(seconds=buf)
    return token.expires_at <= threshold


def shopee_conexao_status(token: ShopeeToken) -> str:
    if not (token.refresh_token or "").strip():
        return "requer_reautorizacao"
    return "conectado"


# -------------------------------------------------
# Helpers de configuração
# -------------------------------------------------
def _get_shopee_api_config() -> tuple[str, int, str]:
    """host, partner_id e partner_key para chamadas de API (não exige redirect)."""
    host, partner_id, partner_key, _, _ = _get_shopee_config(exigir_redirect=False)
    return host, partner_id, partner_key


def _get_shopee_config(exigir_redirect: bool = True):
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

    if not partner_id or not partner_key or (exigir_redirect and not redirect_url):
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
def gerar_auth_url(
    state: Optional[str] = Query(None, alias="state"),
    redirect: bool = Query(
        False,
        description="Se true, redireciona (302) para a Shopee. O link assinado da Shopee expira em minutos; "
        "use este modo para enviar ao seller um link que não expira.",
    ),
):
    host, partner_id, partner_key, redirect_url, env = _get_shopee_config()

    # Se state (sub_base) vier preenchido, colocamos na URL de redirect para o callback receber
    state_val = (state or "").strip()
    if state_val:
        callback = redirect_url + ("&" if "?" in redirect_url else "?") + "state=" + quote(state_val, safe="")
    else:
        callback = redirect_url

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
        f"&redirect={quote(callback, safe='')}"
    )
    if redirect:
        return RedirectResponse(auth_url, status_code=302)
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

    sub_base_val = (state or "").strip() or None

    # Uma conexão por (loja, sub_base): outra base autorizando a mesma loja não toma a conexão existente.
    existente = (
        db.query(ShopeeToken)
        .filter(
            ShopeeToken.shop_id == shop_id,
            ShopeeToken.sub_base == sub_base_val if sub_base_val else ShopeeToken.sub_base.is_(None),
        )
        .order_by(ShopeeToken.id.desc())
        .first()
    )

    if existente:
        existente.access_token = access_token
        existente.refresh_token = refresh_token or existente.refresh_token
        existente.expires_at = expires_at
        existente.main_account_id = main_account_id
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
    success_page = f"{frontend_base}/autenticacao-sucesso.html"
    params = ["shopee=ok"]
    if sub_base_val:
        params.append("sub_base=" + quote(sub_base_val, safe=""))
        owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base_val))
        if owner:
            nome_fantasia = (getattr(owner, "nome_fantasia", None) or "").strip()
            if nome_fantasia:
                params.append("nome_fantasia=" + quote(nome_fantasia, safe=""))
    params.append("_t=" + str(int(time.time())))
    success_url = success_page + "?" + "&".join(params)
    return RedirectResponse(url=success_url, status_code=302)


# -------------------------------------------------
# RefreshAccessToken — renovação agendada (cron ~1h; só tokens expirados)
# -------------------------------------------------
def refresh_shopee_token(db: Session, token: ShopeeToken, *, force: bool = False) -> bool:
    """
    Renova access_token e refresh_token de um ShopeeToken via API RefreshAccessToken.
    Só chama a API quando o token expirou (ou force=True).
    """
    if not (token.refresh_token or "").strip():
        logger.warning("refresh_shopee_token shop_id=%s: sem refresh_token", token.shop_id)
        return False

    if not force and not shopee_token_precisa_renovar(token):
        return True

    lock = _get_shopee_refresh_lock(token.shop_id)
    with lock:
        db.refresh(token)
        if not force and not shopee_token_precisa_renovar(token):
            return True

        try:
            host, partner_id, partner_key = _get_shopee_api_config()
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
            token.expires_at = _utcnow() + timedelta(seconds=int(expires_in))
        db.commit()
        logger.info(
            "refresh_shopee_token: renovado shop_id=%s expira_em=%s",
            token.shop_id,
            token.expires_at.isoformat() if token.expires_at else None,
        )
        return True


def refresh_all_shopee_tokens(db: Session) -> dict[str, int]:
    """Renova ShopeeTokens expirados. Retorna estatísticas para o cron/startup."""
    stats = {"total": 0, "refreshed": 0, "skipped_valid": 0, "failed": 0}
    tokens = db.query(ShopeeToken).filter(ShopeeToken.refresh_token.isnot(None)).all()
    for tk in tokens:
        stats["total"] += 1
        try:
            if not shopee_token_precisa_renovar(tk):
                stats["skipped_valid"] += 1
                continue
            if refresh_shopee_token(db, tk):
                stats["refreshed"] += 1
            else:
                stats["failed"] += 1
        except Exception as e:
            stats["failed"] += 1
            logger.warning("refresh_all_shopee_tokens shop_id=%s: %s", tk.shop_id, e)
    return stats


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
    todos = (
        db.query(ShopeeToken)
        .filter(ShopeeToken.sub_base == sub_base)
        .order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc())
        .all()
    )
    vistos: set[int] = set()
    tokens = []
    for tk in todos:
        if tk.shop_id in vistos:
            continue
        vistos.add(tk.shop_id)
        tokens.append(tk)
    ids_base = {tk.id_base for tk in tokens if tk.id_base is not None}
    nomes_base = {}
    if ids_base:
        nomes_base = {
            b.id_base: b.base
            for b in db.scalars(
                select(BasePreco).where(BasePreco.id_base.in_(ids_base), BasePreco.sub_base == sub_base)
            ).all()
        }
    now = _utcnow()
    result = []
    try:
        host, partner_id, partner_key = _get_shopee_api_config()
    except RuntimeError:
        host = partner_id = partner_key = None
    for tk in tokens:
        status = shopee_conexao_status(tk)
        shop_name = tk.shop_name
        # Preencher nome para tokens antigos que ainda não têm (token válido)
        if not shop_name and status == "conectado" and host and partner_id and partner_key:
            if tk.expires_at and tk.expires_at > now:
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
            "id_base": tk.id_base if tk.id_base in nomes_base else None,
            "base_nome": nomes_base.get(tk.id_base),
        })
    return result


def _exigir_admin_sub_base(user: User) -> str:
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a root e admin.")
    sub_base = (getattr(user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(400, "Usuário sem sub_base.")
    return sub_base


def _tokens_da_loja(db: Session, sub_base: str, shop_id: int) -> list[ShopeeToken]:
    return (
        db.query(ShopeeToken)
        .filter(ShopeeToken.sub_base == sub_base, ShopeeToken.shop_id == shop_id)
        .order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc())
        .all()
    )


def _base_vinculada(db: Session, sub_base: str, id_base: Optional[int]) -> Optional[BasePreco]:
    if id_base is None:
        return None
    return db.scalar(select(BasePreco).where(BasePreco.id_base == id_base, BasePreco.sub_base == sub_base))


class VinculoSellerShopeeIn(BaseModel):
    id_base: Optional[int] = None


# -------------------------------------------------
# 4) Vínculo da loja Shopee com o seller cadastrado
# -------------------------------------------------
@router.patch("/sellers/{shop_id:int}/vinculo")
def shopee_vincular_seller(
    shop_id: int,
    body: VinculoSellerShopeeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Associa (ou desassocia, com id_base=null) a loja Shopee a um seller cadastrado da mesma sub_base."""
    sub_base = _exigir_admin_sub_base(user)
    tokens = _tokens_da_loja(db, sub_base, shop_id)
    if not tokens:
        raise HTTPException(404, "Loja Shopee não conectada a esta sub_base.")
    base_row = None
    if body.id_base is not None:
        base_row = _base_vinculada(db, sub_base, body.id_base)
        if base_row is None:
            raise HTTPException(404, "Seller cadastrado não encontrado nesta sub_base.")
        conflito = (
            db.query(ShopeeToken)
            .filter(
                ShopeeToken.sub_base == sub_base,
                ShopeeToken.id_base == body.id_base,
                ShopeeToken.shop_id != shop_id,
            )
            .order_by(ShopeeToken.criado_em.desc(), ShopeeToken.id.desc())
            .first()
        )
        if conflito is not None:
            nome = (conflito.shop_name or "").strip() or str(conflito.shop_id)
            raise HTTPException(
                409,
                detail={
                    "code": "seller_ja_vinculado",
                    "message": (
                        f'O seller "{base_row.base}" já está vinculado à loja {nome} da Shopee. '
                        'Desvincule essa loja (selecione "Não associado") antes de vincular a esta.'
                    ),
                    "shop_id": conflito.shop_id,
                    "nickname": nome,
                    "base_nome": base_row.base,
                },
            )
    for tk in tokens:
        tk.id_base = body.id_base
    db.commit()
    return {"shop_id": shop_id, "id_base": body.id_base, "base_nome": base_row.base if base_row else None}


# -------------------------------------------------
# 5) Pedidos prontos para coleta: prévia e sincronização
# -------------------------------------------------
@router.get("/sellers/{shop_id:int}/envios")
def shopee_envios_seller(
    shop_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    dias: int = Query(7, ge=1, le=30, description="Janela de pedidos (dias) a considerar."),
    max_pedidos: int = Query(300, ge=50, le=1000),
):
    """
    Prévia (somente leitura) dos pedidos da loja prontos para coleta no canal de entrega da base,
    com indicação se já existem no sistema e se o CEP está na cobertura da sub_base.
    """
    from cobertura_cep_service import cep_coberto, list_prefixos_ativos
    from shopee_int_service import ShopeeApiError, listar_pedidos_pendentes, saidas_existentes_por_pedido

    sub_base = _exigir_admin_sub_base(user)
    tokens = _tokens_da_loja(db, sub_base, shop_id)
    if not tokens:
        raise HTTPException(404, "Loja Shopee não conectada a esta sub_base.")
    try:
        data = listar_pedidos_pendentes(db, tokens[0], dias=dias, max_pedidos=max_pedidos)
    except (LookupError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    except ShopeeApiError as e:
        raise HTTPException(502, f"Erro na API Shopee: {e}")
    except requests.RequestException as e:
        raise HTTPException(502, f"Erro ao conectar na Shopee: {e}")
    ja_registrados = saidas_existentes_por_pedido(db, sub_base, data["envios"])
    prefixos = list_prefixos_ativos(db, sub_base)
    for envio in data["envios"]:
        envio["ja_registrado"] = envio["order_sn"] in ja_registrados
        envio["coberto"] = cep_coberto((envio.get("destinatario") or {}).get("cep"), prefixos)
    base_row = _base_vinculada(db, sub_base, tokens[0].id_base)
    data["vinculado"] = base_row is not None
    data["base_nome"] = base_row.base if base_row else None
    return data


@router.post("/sync-envios")
def shopee_sync_envios(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    shop_id: Optional[int] = Query(None, description="Sincroniza só esta loja; vazio = todas da sub_base."),
    dias: int = Query(7, ge=1, le=30),
    max_pedidos: int = Query(300, ge=50, le=1000),
):
    """
    Para cada loja Shopee vinculada a um seller cadastrado, importa pedidos prontos para coleta
    (canal de entrega da base) como saídas aguardando coleta. Loja sem vínculo é pulada
    com motivo "sem_vinculo".
    """
    from shopee_int_service import sync_envios_sub_base

    sub_base = _exigir_admin_sub_base(user)
    if shop_id is not None and not _tokens_da_loja(db, sub_base, shop_id):
        raise HTTPException(404, "Loja Shopee não conectada a esta sub_base.")
    return sync_envios_sub_base(
        db,
        sub_base,
        shop_id=shop_id,
        user_id=getattr(user, "id", None),
        dias=dias,
        max_pedidos=max_pedidos,
    )