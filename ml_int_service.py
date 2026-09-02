# ml_int_service.py - Serviço ML Int (OAuth, refresh, chamadas API Mercado Livre)
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import requests
from sqlalchemy.orm import Session

from models import MLConexao

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# URLs API Mercado Livre
# -------------------------------------------------------------------
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_ME_URL = "https://api.mercadolibre.com/users/me"
ML_ORDERS_SEARCH_URL = "https://api.mercadolibre.com/orders/search"
ML_ORDER_URL = "https://api.mercadolibre.com/orders"
ML_SHIPMENTS_SEARCH_URL = "https://api.mercadolibre.com/shipments/search"
ML_MARKETPLACE_SHIPMENT_URL = "https://api.mercadolibre.com/marketplace/shipments"
ML_TEST_USER_URL = "https://api.mercadolibre.com/users/test_user"

# Margem operacional antes da expiração (ML recomenda renovar só após perder validade;
# usamos buffer curto para evitar expirar durante uma requisição).
ML_TOKEN_REFRESH_BUFFER_SECONDS = int(os.getenv("ML_TOKEN_REFRESH_BUFFER_SECONDS", "300"))

_refresh_locks: dict[tuple[int, str], threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _utcnow() -> datetime:
    return datetime.utcnow()


def _get_refresh_lock(user_id_ml: int, sub_base: str) -> threading.Lock:
    key = (user_id_ml, (sub_base or "").strip())
    with _refresh_locks_guard:
        if key not in _refresh_locks:
            _refresh_locks[key] = threading.Lock()
        return _refresh_locks[key]


def _get_config() -> tuple[str, str, str]:
    client_id = os.getenv("ML_CLIENT_ID")
    client_secret = os.getenv("ML_CLIENT_SECRET")
    redirect_uri = os.getenv("ML_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REDIRECT_URI devem estar configurados.")
    return client_id, client_secret, redirect_uri


def ml_token_precisa_renovar(conexao: MLConexao, buffer_seconds: int | None = None) -> bool:
    """True se o access_token expirou ou está dentro do buffer de renovação."""
    if not (conexao.refresh_token or "").strip():
        return False
    if not conexao.expires_at:
        return True
    buf = ML_TOKEN_REFRESH_BUFFER_SECONDS if buffer_seconds is None else buffer_seconds
    threshold = _utcnow() + timedelta(seconds=buf)
    return conexao.expires_at <= threshold


def ml_conexao_status(conexao: MLConexao) -> str:
    """
    Status amigável da conexão:
    - conectado: refresh_token presente (renovação automática possível)
    - requer_reautorizacao: sem refresh_token (invalid_grant / revogado / nunca gravado)
    """
    has_refresh = bool((conexao.refresh_token or "").strip())
    if not has_refresh:
        return "requer_reautorizacao"
    return "conectado"


def invalidate_ml_conexao(db: Session, conexao: MLConexao, *, reason: str = "") -> None:
    """
    Marca conexão como inválida: limpa refresh_token para exigir nova autorização.
    Mantém access_token antigo apenas para auditoria (não será usado após expires_at).
    """
    conexao.refresh_token = ""
    conexao.expires_at = _utcnow()
    conexao.atualizado_em = _utcnow()
    db.commit()
    db.refresh(conexao)
    logger.warning(
        "invalidate_ml_conexao: user_id_ml=%s sub_base=%s reason=%s",
        conexao.user_id_ml,
        conexao.sub_base,
        reason or "unknown",
    )


def _resolve_conexao(db: Session, user_id_ml: int, sub_base: str) -> Optional[MLConexao]:
    normalized_sub_base = (sub_base or "").strip() or None
    query = db.query(MLConexao).filter(MLConexao.user_id_ml == user_id_ml)
    if normalized_sub_base is not None:
        query = query.filter(MLConexao.sub_base == normalized_sub_base)
    conexao = query.order_by(MLConexao.criado_em.desc()).first()
    if conexao:
        return conexao
    return (
        db.query(MLConexao)
        .filter(MLConexao.user_id_ml == user_id_ml)
        .order_by(MLConexao.criado_em.desc())
        .first()
    )


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


def create_ml_test_user(site_id: str = "MLB") -> dict[str, Any]:
    """
    Cria um usuário de teste no Mercado Livre (POST /users/test_user).
    Usa o Access Token da aplicação (ML_CLIENT_SECRET das credenciais de teste).
    site_id: MLB (Brasil), MLA (Argentina), etc. Ver API de Sites.
    Retorna: id, nickname, password, site_status. Guarde nickname e password para login.
    """
    token = os.getenv("ML_CLIENT_SECRET")
    if not token:
        raise RuntimeError("ML_CLIENT_SECRET não configurado (necessário para criar usuário de teste).")
    r = requests.post(
        ML_TEST_USER_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"site_id": site_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def refresh_ml_int_token(
    db: Session,
    conexao: MLConexao,
    *,
    force: bool = False,
) -> Optional[MLConexao]:
    """
    Renova access_token usando refresh_token.
    Só chama a API do ML quando o token expirou (ou force=True, ex.: após 401).
    """
    if not (conexao.refresh_token or "").strip():
        logger.warning(
            "refresh_ml_int_token: sem refresh_token user_id_ml=%s sub_base=%s",
            conexao.user_id_ml,
            conexao.sub_base,
        )
        return None

    if not force and not ml_token_precisa_renovar(conexao):
        return conexao

    lock = _get_refresh_lock(conexao.user_id_ml, conexao.sub_base or "")
    with lock:
        db.refresh(conexao)
        if not force and not ml_token_precisa_renovar(conexao):
            return conexao

        client_id, client_secret, _ = _get_config()
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": conexao.refresh_token,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            resp = requests.post(ML_TOKEN_URL, data=data, headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.warning(
                "refresh_ml_int_token: request falhou user_id_ml=%s sub_base=%s: %s",
                conexao.user_id_ml,
                conexao.sub_base,
                e,
            )
            return None

        if resp.status_code != 200:
            body_preview = (resp.text or "")[:300]
            logger.warning(
                "refresh_ml_int_token: ML retornou %s user_id_ml=%s sub_base=%s body=%s",
                resp.status_code,
                conexao.user_id_ml,
                conexao.sub_base,
                body_preview,
            )
            # invalid_grant = refresh já usado, expirado ou autorização revogada no ML
            error_code = ""
            try:
                error_code = str((resp.json() or {}).get("error") or "")
            except Exception:
                error_code = ""
            if resp.status_code in (400, 401) and error_code in ("invalid_grant", "invalid_token"):
                invalidate_ml_conexao(
                    db,
                    conexao,
                    reason=f"ml_{resp.status_code}_{error_code}",
                )
            return None

        payload = resp.json()
        new_refresh = payload.get("refresh_token")
        if not new_refresh:
            logger.warning(
                "refresh_ml_int_token: resposta sem refresh_token user_id_ml=%s sub_base=%s",
                conexao.user_id_ml,
                conexao.sub_base,
            )
            return None

        conexao.access_token = payload["access_token"]
        conexao.refresh_token = new_refresh
        conexao.expires_at = _utcnow() + timedelta(seconds=payload.get("expires_in", 21600))
        conexao.atualizado_em = _utcnow()
        db.commit()
        db.refresh(conexao)
        logger.info(
            "refresh_ml_int_token: renovado user_id_ml=%s sub_base=%s expira_em=%s",
            conexao.user_id_ml,
            conexao.sub_base,
            conexao.expires_at.isoformat() if conexao.expires_at else None,
        )
        return conexao


def get_valid_access_token(db: Session, user_id_ml: int, sub_base: str) -> str:
    """
    Retorna um access_token válido para a conexão do seller.
    Renova somente quando expirado (ou dentro do buffer configurado).
    """
    conexao = _resolve_conexao(db, user_id_ml, sub_base)
    if not conexao:
        raise LookupError(f"Conexão ML não encontrada para user_id_ml={user_id_ml} sub_base={sub_base!r}")

    if not ml_token_precisa_renovar(conexao):
        return conexao.access_token

    refreshed = refresh_ml_int_token(db, conexao)
    if not refreshed:
        if ml_conexao_status(conexao) == "requer_reautorizacao":
            raise RuntimeError("Conexão ML requer nova autorização do seller.")
        raise RuntimeError("Não foi possível renovar o token ML.")
    return refreshed.access_token


def _ml_api_request(
    access_token: str,
    request_fn: Callable[[str], requests.Response],
    *,
    db: Optional[Session] = None,
    user_id_ml: Optional[int] = None,
    sub_base: Optional[str] = None,
) -> requests.Response:
    """Executa request à API ML; em 401 tenta renovar o token e repetir uma vez."""
    resp = request_fn(access_token)
    if resp.status_code != 401 or db is None or user_id_ml is None:
        return resp

    conexao = _resolve_conexao(db, user_id_ml, sub_base or "")
    if not conexao:
        return resp

    refreshed = refresh_ml_int_token(db, conexao, force=True)
    if not refreshed:
        return resp

    return request_fn(refreshed.access_token)


def fetch_orders_search(
    access_token: str,
    seller_id: int,
    *,
    order_status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    db: Optional[Session] = None,
    sub_base: Optional[str] = None,
) -> dict[str, Any]:
    """GET /orders/search com seller e filtros opcionais."""
    params: dict[str, Any] = {"seller": seller_id, "offset": offset, "limit": limit}
    if order_status:
        params["order.status"] = order_status
    if date_from:
        params["order.date_created.from"] = date_from
    if date_to:
        params["order.date_created.to"] = date_to

    def do_req(token: str) -> requests.Response:
        return requests.get(
            ML_ORDERS_SEARCH_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )

    r = _ml_api_request(
        access_token,
        do_req,
        db=db,
        user_id_ml=seller_id,
        sub_base=sub_base,
    )
    r.raise_for_status()
    return r.json()


def fetch_shipment(
    access_token: str,
    shipment_id: int,
    *,
    db: Optional[Session] = None,
    user_id_ml: Optional[int] = None,
    sub_base: Optional[str] = None,
) -> dict[str, Any]:
    """GET /marketplace/shipments/{id} com header x-format-new."""
    url = f"{ML_MARKETPLACE_SHIPMENT_URL}/{shipment_id}"

    def do_req(token: str) -> requests.Response:
        return requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "x-format-new": "true"},
            timeout=15,
        )

    r = _ml_api_request(
        access_token,
        do_req,
        db=db,
        user_id_ml=user_id_ml,
        sub_base=sub_base,
    )
    r.raise_for_status()
    return r.json()


def fetch_order(
    access_token: str,
    order_id: str,
    *,
    db: Optional[Session] = None,
    user_id_ml: Optional[int] = None,
    sub_base: Optional[str] = None,
) -> dict[str, Any]:
    """GET /orders/{id} - retorna pedido com shipping.id quando houver."""

    def do_req(token: str) -> requests.Response:
        return requests.get(
            f"{ML_ORDER_URL}/{order_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )

    r = _ml_api_request(
        access_token,
        do_req,
        db=db,
        user_id_ml=user_id_ml,
        sub_base=sub_base,
    )
    r.raise_for_status()
    return r.json()


def fetch_shipments_by_tracking(
    access_token: str,
    tracking_number: str,
    *,
    db: Optional[Session] = None,
    user_id_ml: Optional[int] = None,
    sub_base: Optional[str] = None,
) -> dict[str, Any]:
    """GET /shipments/search?tracking_number=..."""

    def do_req(token: str) -> requests.Response:
        return requests.get(
            ML_SHIPMENTS_SEARCH_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"tracking_number": tracking_number},
            timeout=15,
        )

    r = _ml_api_request(
        access_token,
        do_req,
        db=db,
        user_id_ml=user_id_ml,
        sub_base=sub_base,
    )
    r.raise_for_status()
    return r.json()


def refresh_all_ml_int_tokens(db: Session) -> dict[str, int]:
    """
    Varre ml_conexoes e renova apenas tokens expirados (ou no buffer).
    Retorna estatísticas para o cron/startup.
    """
    stats = {
        "total": 0,
        "refreshed": 0,
        "skipped_valid": 0,
        "skipped_invalid": 0,
        "failed": 0,
    }
    conexoes = db.query(MLConexao).all()
    for c in conexoes:
        stats["total"] += 1
        try:
            if not (c.refresh_token or "").strip():
                stats["skipped_invalid"] += 1
                continue
            if not ml_token_precisa_renovar(c):
                stats["skipped_valid"] += 1
                continue
            if refresh_ml_int_token(db, c) is not None:
                stats["refreshed"] += 1
            else:
                stats["failed"] += 1
        except Exception as e:
            stats["failed"] += 1
            logger.warning(
                "refresh_all_ml_int_tokens: falha user_id_ml=%s sub_base=%s: %s",
                c.user_id_ml,
                c.sub_base,
                e,
            )
    return stats
