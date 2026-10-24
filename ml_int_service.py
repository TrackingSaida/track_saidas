# ml_int_service.py - Serviço ML Int (OAuth, refresh, chamadas API Mercado Livre)
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import requests
from sqlalchemy import func, or_, select, text
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
ML_SHIPMENT_URL = "https://api.mercadolibre.com/shipments"
ML_SHIPMENT_LABELS_URL = "https://api.mercadolibre.com/shipment_labels"
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
    sort: Optional[str] = None,
    db: Optional[Session] = None,
    sub_base: Optional[str] = None,
) -> dict[str, Any]:
    """GET /orders/search com seller e filtros opcionais. `results` são objetos de pedido."""
    params: dict[str, Any] = {"seller": seller_id, "offset": offset, "limit": limit}
    if sort:
        params["sort"] = sort
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
    """GET /shipments/{id} com header x-format-new (destination.shipping_address)."""
    url = f"{ML_SHIPMENT_URL}/{shipment_id}"

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


def _pdf_de_conteudo_ml(conteudo: bytes) -> Optional[bytes]:
    """PDF cru ou o primeiro PDF dentro do ZIP que o Mercado Livre devolve."""
    import io
    import zipfile

    if conteudo.startswith(b"%PDF"):
        return conteudo
    if conteudo[:2] != b"PK":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
            for nome in zf.namelist():
                dados = zf.read(nome)
                if dados.startswith(b"%PDF"):
                    return dados
    except zipfile.BadZipFile:
        return None
    return None


def fetch_shipment_label_pdf(
    access_token: str,
    shipment_id: int,
    *,
    db: Optional[Session] = None,
    user_id_ml: Optional[int] = None,
    sub_base: Optional[str] = None,
) -> bytes:
    """GET /shipment_labels (pdf): PDF oficial da etiqueta Flex. Erra se o corpo não for PDF."""

    def do_req(token: str) -> requests.Response:
        return requests.get(
            ML_SHIPMENT_LABELS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"shipment_ids": shipment_id, "response_type": "pdf"},
            timeout=20,
        )

    r = _ml_api_request(access_token, do_req, db=db, user_id_ml=user_id_ml, sub_base=sub_base)
    r.raise_for_status()
    pdf = _pdf_de_conteudo_ml(r.content or b"")
    if not pdf:
        raise ValueError(f"shipment_labels sem PDF para shipment {shipment_id}")
    return pdf


def fetch_shipment_label_zpl(
    access_token: str,
    shipment_id: int,
    *,
    db: Optional[Session] = None,
    user_id_ml: Optional[int] = None,
    sub_base: Optional[str] = None,
) -> str:
    """GET /shipment_labels (zpl2): ZPL da etiqueta oficial; o ML pode devolver ZIP com o .txt."""
    import io
    import zipfile

    def do_req(token: str) -> requests.Response:
        return requests.get(
            ML_SHIPMENT_LABELS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"shipment_ids": shipment_id, "response_type": "zpl2"},
            timeout=20,
        )

    r = _ml_api_request(access_token, do_req, db=db, user_id_ml=user_id_ml, sub_base=sub_base)
    r.raise_for_status()
    conteudo = r.content or b""
    if conteudo[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
            return "\n".join(zf.read(n).decode("utf-8", errors="replace") for n in zf.namelist())
    return conteudo.decode("utf-8", errors="replace")


def _conexoes_etiqueta_ml(db: Session, saida: Any) -> list[MLConexao]:
    """Contas ML da sub_base do pedido, vinculadas a um seller. A da base do pedido vem primeiro."""
    from models import BasePreco

    sub_base = (getattr(saida, "sub_base", None) or "").strip()
    if not sub_base or not getattr(saida, "ml_shipment_id", None):
        return []
    base_nome = (getattr(saida, "base", None) or "").strip().upper()
    id_base_saida = None
    if base_nome:
        id_base_saida = db.scalar(
            select(BasePreco.id_base).where(
                BasePreco.sub_base == sub_base,
                func.upper(func.trim(BasePreco.base)) == base_nome,
            ).limit(1)
        )
    conexoes = (
        db.query(MLConexao)
        .filter(MLConexao.sub_base == sub_base, MLConexao.id_base.isnot(None))
        .order_by(MLConexao.criado_em.desc(), MLConexao.id.desc())
        .all()
    )
    conexoes.sort(key=lambda c: c.id_base != id_base_saida)
    vistos: set[int] = set()
    unicas: list[MLConexao] = []
    for conexao in conexoes:
        if conexao.user_id_ml in vistos:
            continue
        vistos.add(conexao.user_id_ml)
        unicas.append(conexao)
    return unicas


def _baixar_da_conta_ml(db: Session, saida: Any, baixar: Callable[[str, MLConexao, int], Any], origem: str) -> Any:
    """Tenta cada conta da sub_base até a primeira resposta útil. None se nenhuma servir."""
    sid = getattr(saida, "ml_shipment_id", None)
    sub_base = (getattr(saida, "sub_base", None) or "").strip()
    if not sid or not sub_base:
        return None
    for conexao in _conexoes_etiqueta_ml(db, saida):
        try:
            token = get_valid_access_token(db, conexao.user_id_ml, sub_base)
            valor = baixar(token, conexao, int(sid))
        except Exception as e:
            logger.info("%s: shipment=%s user_id_ml=%s indisponível: %s", origem, sid, conexao.user_id_ml, e)
            continue
        if valor:
            return valor
    return None


def obter_pdf_etiqueta_ml(db: Session, saida: Any) -> Optional[bytes]:
    """PDF oficial do envio. None se não houver conta da sub_base ou o Mercado Livre não devolver PDF."""

    def baixar(token: str, conexao: MLConexao, sid: int) -> bytes:
        return fetch_shipment_label_pdf(
            token, sid, db=db, user_id_ml=conexao.user_id_ml, sub_base=conexao.sub_base,
        )

    return _baixar_da_conta_ml(db, saida, baixar, "obter_pdf_etiqueta_ml")


def obter_qr_etiqueta_ml(db: Session, saida: Any) -> Optional[str]:
    """
    JSON do QR oficial do envio ML de uma saída sincronizada, lido da etiqueta do seller
    (conta ML da sub_base vinculada à base da saída primeiro). None se indisponível.
    Baixar a etiqueta pode marcar o envio como impresso no painel do seller.
    """
    from qr_payload_utils import extrair_qr_ml_de_zpl

    def baixar(token: str, conexao: MLConexao, sid: int) -> Optional[str]:
        zpl = fetch_shipment_label_zpl(
            token, sid, db=db, user_id_ml=conexao.user_id_ml, sub_base=conexao.sub_base,
        )
        return extrair_qr_ml_de_zpl(zpl, sid)

    return _baixar_da_conta_ml(db, saida, baixar, "obter_qr_etiqueta_ml")


ML_ORDERS_PAGE_SIZE = 50


def _order_itens(order: dict) -> list[dict[str, Any]]:
    itens = []
    for oi in order.get("order_items") or []:
        item = oi.get("item") or {}
        itens.append({"titulo": item.get("title"), "quantidade": oi.get("quantity")})
    return itens


def listar_envios_flex_pendentes(
    db: Session,
    user_id_ml: int,
    sub_base: str,
    *,
    dias: int = 7,
    max_pedidos: int = 300,
) -> dict[str, Any]:
    """
    Envios Flex do seller prontos para coleta (shipment ready_to_ship + self_service),
    com destinatário normalizado. Pedidos de um mesmo pack compartilham o shipment.
    Lança LookupError/RuntimeError (conexão) ou requests.HTTPError (busca de pedidos).
    """
    from codigo_normalizer import normalize_codigo
    from ml_int_envios_pure import (
        ml_date_from,
        ml_order_shipment_id,
        ml_shipment_destinatario,
        ml_shipment_logistic_type,
        ml_shipment_pendente_coleta,
    )

    token = get_valid_access_token(db, user_id_ml, sub_base)
    resumo = {"pedidos_lidos": 0, "envios_consultados": 0, "pendentes": 0, "ignorados": 0}
    erros: list[dict[str, Any]] = []
    pedidos_por_envio: dict[int, list[dict]] = {}

    offset = 0
    date_from = ml_date_from(dias)
    while offset < max_pedidos:
        page = fetch_orders_search(
            token,
            user_id_ml,
            order_status="paid",
            date_from=date_from,
            offset=offset,
            limit=ML_ORDERS_PAGE_SIZE,
            sort="date_desc",
            db=db,
            sub_base=sub_base,
        )
        results = page.get("results") or []
        for order in results:
            resumo["pedidos_lidos"] += 1
            sid = ml_order_shipment_id(order)
            if sid:
                pedidos_por_envio.setdefault(sid, []).append(order)
        total = (page.get("paging") or {}).get("total", 0)
        offset += ML_ORDERS_PAGE_SIZE
        if not results or offset >= total:
            break

    envios: list[dict[str, Any]] = []
    for sid, orders in pedidos_por_envio.items():
        resumo["envios_consultados"] += 1
        try:
            ship = fetch_shipment(token, sid, db=db, user_id_ml=user_id_ml, sub_base=sub_base)
        except requests.HTTPError as e:
            erros.append({"shipment_id": sid, "erro": f"HTTP {e.response.status_code}"})
            continue
        except requests.RequestException as e:
            erros.append({"shipment_id": sid, "erro": str(e)})
            continue
        if not ml_shipment_pendente_coleta(ship):
            resumo["ignorados"] += 1
            continue
        resumo["pendentes"] += 1
        codigo = normalize_codigo(str(sid), strict_qr=False)[0] or str(sid)
        envios.append({
            "shipment_id": sid,
            "codigo": codigo,
            "order_ids": [o.get("id") for o in orders],
            "pack_id": orders[0].get("pack_id"),
            "data_venda": orders[0].get("date_created"),
            "status": ship.get("status"),
            "substatus": ship.get("substatus"),
            "logistic_type": ml_shipment_logistic_type(ship),
            "tracking_number": ship.get("tracking_number"),
            "itens": [i for o in orders for i in _order_itens(o)],
            "destinatario": ml_shipment_destinatario(ship),
        })

    return {"seller_id": user_id_ml, "dias": dias, "resumo": resumo, "envios": envios, "erros": erros}


def _lock_sync_sub_base(db: Session, sub_base: str) -> None:
    """Serializa syncs concorrentes (cron x botão) da mesma sub_base até o commit."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"ml_int_sync:{sub_base}"},
        )


def saidas_existentes_por_shipment(db: Session, sub_base: str, envios: list[dict]) -> dict[int, Any]:
    """shipment_id → Saida já existente na sub_base (por ml_shipment_id ou código lido no scanner)."""
    from models import Saida

    if not envios:
        return {}
    por_codigo = {e["codigo"]: e["shipment_id"] for e in envios}
    sids = [e["shipment_id"] for e in envios]
    rows = db.scalars(
        select(Saida).where(
            Saida.sub_base == sub_base,
            or_(Saida.ml_shipment_id.in_(sids), Saida.codigo.in_(list(por_codigo))),
        )
    ).all()
    existentes: dict[int, Any] = {}
    for row in rows:
        if row.ml_shipment_id in sids:
            existentes.setdefault(row.ml_shipment_id, row)
        elif row.codigo in por_codigo:
            existentes.setdefault(por_codigo[row.codigo], row)
    return existentes


_CAMPOS_COORDENADA = ("latitude", "longitude", "coord_precision", "geocode_source", "endereco_origem", "geocoded_at")


def _completar_saida_existente(db: Session, saida: Any, envio: dict, detail_fields: dict) -> bool:
    """Preenche dados ML/destinatário que faltam numa saída já registrada (não sobrescreve)."""
    from models import SaidaDetail
    from ml_int_envios_pure import campos_faltantes

    from ml_int_envios_pure import data_venda_local

    order_ids = envio.get("order_ids") or []
    ml_novos = {
        "ml_shipment_id": envio["shipment_id"],
        "ml_order_id": order_ids[0] if order_ids else None,
        "ml_pack_id": envio.get("pack_id"),
        "data_venda": data_venda_local(envio.get("data_venda")),
    }
    ml_atual = {k: getattr(saida, k) for k in ml_novos}
    alterou = False
    for k, v in campos_faltantes(ml_atual, {k: v for k, v in ml_novos.items() if v is not None}).items():
        setattr(saida, k, v)
        alterou = True

    detail = db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == saida.id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )
    if detail is not None:
        novos = dict(detail_fields)
        if detail.latitude is not None or detail.longitude is not None:
            for k in _CAMPOS_COORDENADA:
                novos.pop(k, None)
        detail_atual = {k: getattr(detail, k, None) for k in novos}
        for k, v in campos_faltantes(detail_atual, novos).items():
            setattr(detail, k, v)
            alterou = True
    return alterou


def sync_envios_sub_base(
    db: Session,
    sub_base: str,
    *,
    user_id_ml: Optional[int] = None,
    user_id: Optional[int] = None,
    dias: int = 7,
    max_pedidos: int = 300,
) -> dict[str, Any]:
    """
    Importa envios Flex prontos para coleta dos sellers ML vinculados da sub_base:
    cria Saida (aguardando_coleta) + SaidaDetail (destinatário/coordenada) e completa
    dados ML faltantes de saídas já registradas. Conta sem vínculo com seller é pulada.
    Com cobertura de CEP configurada na sub_base, envio novo fora da área é rejeitado
    (não importado) e listado em "fora_cobertura"; sem cobertura configurada, importa tudo.
    Usado pelo POST /ml-int/sync-envios e pelo cron interno.
    """
    from cobertura_cep_service import cep_coberto, list_prefixos_ativos
    from models import BasePreco, Saida, SaidaDetail, SaidaHistorico
    from ml_int_envios_pure import data_venda_local, destinatario_to_saida_detail_fields, envio_fora_cobertura_resumo

    prefixos_cobertura = list_prefixos_ativos(db, sub_base)

    q = db.query(MLConexao).filter(MLConexao.sub_base == sub_base)
    if user_id_ml is not None:
        q = q.filter(MLConexao.user_id_ml == user_id_ml)
    conexoes = q.order_by(MLConexao.criado_em.desc(), MLConexao.id.desc()).all()

    vistos: set[int] = set()
    sellers: list[dict[str, Any]] = []
    total_criados = 0
    for conexao in conexoes:
        if conexao.user_id_ml in vistos:
            continue
        vistos.add(conexao.user_id_ml)
        seller: dict[str, Any] = {
            "user_id_ml": conexao.user_id_ml,
            "nickname": conexao.user_nickname_ml,
            "id_base": conexao.id_base,
            "base_nome": None,
            "motivo": None,
            "pendentes": 0,
            "criados": 0,
            "ja_registrados": 0,
            "atualizados": 0,
            "fora_cobertura": [],
            "erros": [],
        }
        sellers.append(seller)

        base_row = None
        if conexao.id_base is not None:
            base_row = db.scalar(
                select(BasePreco).where(
                    BasePreco.id_base == conexao.id_base,
                    BasePreco.sub_base == sub_base,
                )
            )
        if base_row is None or not (base_row.base or "").strip():
            seller["motivo"] = "sem_vinculo"
            continue
        base_nome = base_row.base.strip()
        seller["base_nome"] = base_nome

        try:
            data = listar_envios_flex_pendentes(
                db, conexao.user_id_ml, sub_base, dias=dias, max_pedidos=max_pedidos,
            )
        except (LookupError, RuntimeError) as e:
            seller["motivo"] = "requer_reautorizacao" if "autoriza" in str(e) else "erro_conexao"
            seller["erros"].append({"erro": str(e)})
            continue
        except requests.HTTPError as e:
            seller["motivo"] = "erro_api_ml"
            seller["erros"].append({"erro": f"HTTP {e.response.status_code}: {(e.response.text or '')[:200]}"})
            continue
        except requests.RequestException as e:
            seller["motivo"] = "erro_api_ml"
            seller["erros"].append({"erro": str(e)[:200]})
            continue

        seller["erros"].extend(data["erros"])
        envios = data["envios"]
        seller["pendentes"] = len(envios)
        if not envios:
            continue

        agora = _utcnow()
        try:
            _lock_sync_sub_base(db, sub_base)
            existentes = saidas_existentes_por_shipment(db, sub_base, envios)
            for envio in envios:
                sid = envio["shipment_id"]
                detail_fields = destinatario_to_saida_detail_fields(envio["destinatario"])
                if "latitude" in detail_fields:
                    detail_fields["geocoded_at"] = agora
                existente = existentes.get(sid)
                if existente is not None:
                    seller["ja_registrados"] += 1
                    try:
                        with db.begin_nested():
                            if _completar_saida_existente(db, existente, envio, detail_fields):
                                seller["atualizados"] += 1
                    except Exception as e:
                        seller["erros"].append({"shipment_id": sid, "erro": str(e)[:200]})
                    continue
                if not cep_coberto((envio.get("destinatario") or {}).get("cep"), prefixos_cobertura):
                    seller["fora_cobertura"].append(envio_fora_cobertura_resumo(envio))
                    continue
                order_ids = envio.get("order_ids") or []
                try:
                    with db.begin_nested():
                        nova = Saida(
                            sub_base=sub_base,
                            codigo=envio["codigo"],
                            servico="Mercado Livre",
                            status="aguardando_coleta",
                            ml_shipment_id=sid,
                            ml_order_id=order_ids[0] if order_ids else None,
                            ml_pack_id=envio.get("pack_id"),
                            data_venda=data_venda_local(envio.get("data_venda")),
                            base=base_nome,
                        )
                        db.add(nova)
                        db.flush()
                        db.add(
                            SaidaDetail(
                                id_saida=nova.id_saida,
                                id_entregador=None,
                                status="Aguardando coleta",
                                **detail_fields,
                            )
                        )
                        db.add(
                            SaidaHistorico(
                                id_saida=nova.id_saida,
                                evento="ml_int_sync",
                                status_novo="aguardando_coleta",
                                user_id=user_id,
                            )
                        )
                    seller["criados"] += 1
                except Exception as e:
                    seller["erros"].append({"shipment_id": sid, "erro": str(e)[:200]})
            db.commit()
        except Exception as e:
            db.rollback()
            seller["criados"] = 0
            seller["atualizados"] = 0
            seller["erros"].append({"erro": f"Falha ao gravar: {str(e)[:200]}"})
            continue
        total_criados += seller["criados"]
        if seller["fora_cobertura"]:
            logger.info(
                "ml_int_sync: sub_base=%s user_id_ml=%s rejeitados_fora_cobertura=%s shipments=%s",
                sub_base,
                conexao.user_id_ml,
                len(seller["fora_cobertura"]),
                [f["shipment_id"] for f in seller["fora_cobertura"]],
            )

    return {"sub_base": sub_base, "criados": total_criados, "dias": dias, "sellers": sellers}


def sync_envios_todas_sub_bases(
    db: Session,
    *,
    dias: int = 7,
    max_runtime_seconds: int = 240,
) -> dict[str, Any]:
    """Cron: sincroniza todas as sub_bases com conta ML vinculada a seller, com orçamento de tempo."""
    import time

    inicio = time.monotonic()
    sub_bases = [
        sb
        for (sb,) in db.query(MLConexao.sub_base)
        .filter(MLConexao.id_base.isnot(None))
        .distinct()
        .order_by(MLConexao.sub_base)
        .all()
        if (sb or "").strip()
    ]
    resultados: list[dict[str, Any]] = []
    pendentes_tempo: list[str] = []
    for sb in sub_bases:
        if time.monotonic() - inicio > max_runtime_seconds:
            pendentes_tempo.append(sb)
            continue
        try:
            resultados.append(sync_envios_sub_base(db, sb, dias=dias))
        except Exception as e:
            db.rollback()
            logger.warning("sync_envios_todas_sub_bases: falha sub_base=%s: %s", sb, e)
            resultados.append({"sub_base": sb, "criados": 0, "erro": str(e)[:200]})
    return {
        "sub_bases": len(sub_bases),
        "criados": sum(r.get("criados", 0) for r in resultados),
        "partial": bool(pendentes_tempo),
        "nao_processadas": pendentes_tempo,
        "resultados": resultados,
    }


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
