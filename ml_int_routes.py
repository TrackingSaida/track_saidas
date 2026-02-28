# ml_int_routes.py - Rotas ML Int (OAuth, sellers, envios, volume, etiqueta)
from __future__ import annotations

import os
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from sqlalchemy import select

from auth import get_current_user
from db import get_db
from models import MLConexao, Saida, SaidaDetail, SaidaHistorico, User
from ml_int_service import (
    exchange_code_for_token,
    fetch_order,
    fetch_orders_search,
    fetch_shipment,
    fetch_shipments_by_tracking,
    get_me,
    get_valid_access_token,
)

router = APIRouter(prefix="/ml-int", tags=["ML Int"])

ML_AUTH_BASE = "https://auth.mercadolivre.com.br/authorization"
ML_REDIRECT_URI_INT = os.getenv("ML_REDIRECT_URI_ML_INT") or os.getenv("ML_REDIRECT_URI")
FRONTEND_AFTER_CALLBACK = os.getenv("ML_AFTER_CALLBACK", "https://tracking-saidas.com.br/")


# ---------- OAuth: connect (sem auth) ----------
@router.get("/connect")
def ml_int_connect(state: str = Query("", alias="state")):
    """Gera URL de autorização ML. Frontend deve passar state=sub_base (ou valor codificado)."""
    client_id = os.getenv("ML_CLIENT_ID")
    redirect_uri = ML_REDIRECT_URI_INT
    if not client_id or not redirect_uri:
        raise HTTPException(500, "ML_CLIENT_ID ou ML_REDIRECT_URI não configurados.")
    params = f"response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
    if state:
        params += f"&state={state}"
    return {"auth_url": f"{ML_AUTH_BASE}?{params}"}


# ---------- OAuth: callback (sem auth) ----------
@router.get("/callback")
def ml_int_callback(
    code: str = Query(...),
    state: str = Query(""),
    db: Session = Depends(get_db),
):
    """Troca code por token, grava ml_conexoes com sub_base = state, redireciona para sucesso."""
    if not ML_REDIRECT_URI_INT:
        raise HTTPException(500, "ML_REDIRECT_URI não configurado.")
    try:
        token_data = exchange_code_for_token(code, ML_REDIRECT_URI_INT)
    except requests.HTTPError as e:
        raise HTTPException(e.response.status_code, e.response.text or "Erro ao obter token")
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    user_me = get_me(access_token)
    user_id_ml = user_me["id"]

    existente = (
        db.query(MLConexao)
        .filter(MLConexao.user_id_ml == user_id_ml, MLConexao.sub_base == (state or "").strip() or None)
        .first()
    )
    base_url = (FRONTEND_AFTER_CALLBACK or "").rstrip("/")
    success_page = f"{base_url}/autenticacao-sucesso.html"

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    sub_base_value = (state or "").strip() or None

    if existente:
        existente.access_token = access_token
        existente.refresh_token = refresh_token
        existente.expires_at = expires_at
        existente.atualizado_em = datetime.utcnow()
        if sub_base_value is not None:
            existente.sub_base = sub_base_value
        db.commit()
        return RedirectResponse(url=f"{success_page}?ml=ok")
    novo = MLConexao(
        sub_base=sub_base_value or "",
        user_id_ml=user_id_ml,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    db.add(novo)
    db.commit()
    return RedirectResponse(url=f"{success_page}?ml=ok")


# ---------- Sellers (auth, filtro por sub_base) ----------
@router.get("/sellers")
def ml_int_sellers(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista conexões (sellers) da sub_base do usuário."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a root e admin.")
    sub_base = getattr(user, "sub_base", None)
    q = db.query(MLConexao).filter(MLConexao.sub_base == sub_base)
    conexoes = q.order_by(MLConexao.criado_em.desc()).all()
    now = datetime.utcnow()
    return [
        {
            "id": c.id,
            "user_id_ml": c.user_id_ml,
            "sub_base": c.sub_base,
            "status": "conectado" if (c.expires_at and c.expires_at > now) else "expirado",
            "criado_em": c.criado_em.isoformat() if c.criado_em else None,
        }
        for c in conexoes
    ]


# ---------- Volume pendente (auth) ----------
@router.get("/sellers/{user_id_ml:int}/volume-pendente")
def ml_int_volume_pendente(
    user_id_ml: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pedidos do seller ainda não entregues (para dimensionar coleta)."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    try:
        token = get_valid_access_token(db, user_id_ml, sub_base)
    except (LookupError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    # Status que indicam pedido não entregue: paid, ready_to_ship, in_transit, etc.
    try:
        data = fetch_orders_search(
            token,
            user_id_ml,
            order_status="paid",
            limit=100,
        )
    except requests.HTTPError as e:
        raise HTTPException(e.response.status_code, e.response.text or "Erro na API ML")
    results = data.get("results") or []
    total = data.get("paging", {}).get("total", len(results))
    return {"total": total, "results": results, "seller_id": user_id_ml}


# ---------- Envios do seller (tracking + destination) ----------
@router.get("/sellers/{user_id_ml:int}/envios")
def ml_int_envios_seller(
    user_id_ml: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=100),
):
    """Lista envios do seller com tracking e destinatário (para roteirização)."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    try:
        token = get_valid_access_token(db, user_id_ml, sub_base)
    except (LookupError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    try:
        search = fetch_orders_search(token, user_id_ml, limit=limit)
    except requests.HTTPError as e:
        raise HTTPException(e.response.status_code, e.response.text or "Erro na API ML")
    results = search.get("results") or []
    envios = []
    seen_shipment = set()
    for order_id in results[:limit]:
        try:
            order = fetch_order(token, str(order_id))
        except Exception:
            continue
        shipping = order.get("shipping") or {}
        sid = shipping.get("id")
        if not sid or sid in seen_shipment:
            continue
        seen_shipment.add(sid)
        try:
            ship = fetch_shipment(token, int(sid))
        except Exception:
            continue
        dest = ship.get("destination") or {}
        envios.append({
            "shipment_id": sid,
            "order_id": order.get("id"),
            "tracking_number": ship.get("tracking_number"),
            "status": ship.get("status"),
            "substatus": ship.get("substatus"),
            "destination": {
                "address_line": dest.get("address_line"),
                "street_name": dest.get("street_name"),
                "street_number": dest.get("street_number"),
                "city": dest.get("city"),
                "state": dest.get("state"),
                "zip_code": dest.get("zip_code"),
                "receiver_name": dest.get("receiver_name"),
            },
        })
    return {"envios": envios, "seller_id": user_id_ml}


# ---------- Detalhe envio por id ou tracking ----------
@router.get("/envios/{shipment_id:int}")
def ml_int_envio_detail(
    shipment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Detalhes do envio (roteirização). Requer que o shipment pertença a um seller conectado à sub_base."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    conexoes = db.query(MLConexao).filter(MLConexao.sub_base == sub_base).all()
    if not conexoes:
        raise HTTPException(404, "Nenhuma conexão ML para esta sub_base.")
    last_error = None
    for c in conexoes:
        try:
            token = get_valid_access_token(db, c.user_id_ml, sub_base)
            ship = fetch_shipment(token, shipment_id)
            dest = ship.get("destination") or {}
            return {
                "shipment_id": shipment_id,
                "tracking_number": ship.get("tracking_number"),
                "status": ship.get("status"),
                "substatus": ship.get("substatus"),
                "destination": dest,
                "origin": ship.get("origin"),
            }
        except Exception as e:
            last_error = e
            continue
    raise HTTPException(404, str(last_error) if last_error else "Envio não encontrado.")


@router.get("/envios")
def ml_int_envio_by_tracking(
    tracking_number: str = Query(..., description="Código de rastreio"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Busca envio por tracking_number. Retorna primeiro encontrado entre os sellers da sub_base."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    conexoes = db.query(MLConexao).filter(MLConexao.sub_base == sub_base).all()
    for c in conexoes:
        try:
            token = get_valid_access_token(db, c.user_id_ml, sub_base)
            data = fetch_shipments_by_tracking(token, tracking_number)
            results = data.get("results") or []
            if not results:
                continue
            sid = results[0].get("id")
            if sid:
                ship = fetch_shipment(token, int(sid))
                dest = ship.get("destination") or {}
                return {
                    "shipment_id": sid,
                    "tracking_number": ship.get("tracking_number"),
                    "status": ship.get("status"),
                    "substatus": ship.get("substatus"),
                    "destination": dest,
                }
        except Exception:
            continue
    raise HTTPException(404, f"Nenhum envio encontrado para tracking '{tracking_number}'.")


# ---------- Etiqueta (placeholder até confirmar endpoint BR) ----------
@router.get("/envios/{shipment_id:int}/etiqueta")
def ml_int_etiqueta(
    shipment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Geração/reimpressão de etiqueta. Endpoint oficial BR a confirmar na doc ML."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    conexoes = db.query(MLConexao).filter(MLConexao.sub_base == sub_base).all()
    for c in conexoes:
        try:
            token = get_valid_access_token(db, c.user_id_ml, sub_base)
            ship = fetch_shipment(token, shipment_id)
            # Doc ML BR: endpoint de etiqueta a confirmar; por ora retornar dados do envio
            return {
                "shipment_id": shipment_id,
                "tracking_number": ship.get("tracking_number"),
                "status": ship.get("status"),
                "message": "Endpoint de PDF/URL de etiqueta a implementar conforme doc Mercado Envíos BR.",
            }
        except Exception:
            continue
    raise HTTPException(404, "Envio não encontrado.")


# ---------- Sync: preenchimento automático (saidas + saidas_detail) ----------
def _destination_to_dest_fields(dest: dict) -> dict:
    """Mapeia destination da API ML para campos dest_* do SaidaDetail."""
    if not dest:
        return {}
    return {
        "dest_nome": dest.get("receiver_name") or dest.get("receiver_name"),
        "dest_rua": dest.get("street_name"),
        "dest_numero": str(dest.get("street_number") or "") if dest.get("street_number") is not None else None,
        "dest_bairro": dest.get("neighborhood") or dest.get("municipality"),
        "dest_cidade": dest.get("city"),
        "dest_estado": dest.get("state"),
        "dest_cep": dest.get("zip_code"),
        "dest_contato": dest.get("receiver_phone"),
        "endereco_formatado": dest.get("address_line"),
    }


@router.post("/sync-envios")
def ml_int_sync_envios(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Job/endpoint: para cada seller conectado à sub_base, busca envios 'ready_to_ship'
    ainda não registrados e cria Saida (aguardando_coleta) + SaidaDetail (dest_*).
    """
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    if not sub_base:
        raise HTTPException(400, "Usuário sem sub_base.")
    conexoes = db.query(MLConexao).filter(MLConexao.sub_base == sub_base).all()
    created = 0
    errors = []
    for conexao in conexoes:
        try:
            token = get_valid_access_token(db, conexao.user_id_ml, sub_base)
        except Exception as e:
            errors.append({"user_id_ml": conexao.user_id_ml, "error": str(e)})
            continue
        try:
            search = fetch_orders_search(token, conexao.user_id_ml, limit=50)
        except requests.HTTPError as e:
            errors.append({"user_id_ml": conexao.user_id_ml, "error": e.response.text or str(e)})
            continue
        results = search.get("results") or []
        for order_id in results:
            try:
                order = fetch_order(token, str(order_id))
            except Exception:
                continue
            shipping = order.get("shipping") or {}
            sid = shipping.get("id")
            if not sid:
                continue
            # Já registrado?
            existing = db.scalar(select(Saida).where(Saida.ml_shipment_id == sid))
            if existing:
                continue
            try:
                ship = fetch_shipment(token, int(sid))
            except Exception:
                continue
            status_ship = (ship.get("status") or "").lower()
            substatus = (ship.get("substatus") or "").lower()
            # Disponível para coleta: ready_to_ship ou equivalente
            if status_ship not in ("ready_to_ship", "ready"):
                continue
            tracking = ship.get("tracking_number") or ""
            dest = ship.get("destination") or {}
            dest_fields = _destination_to_dest_fields(dest)
            try:
                nova = Saida(
                    sub_base=sub_base,
                    codigo=tracking,
                    status="aguardando_coleta",
                    ml_shipment_id=sid,
                    ml_order_id=order.get("id"),
                    base=order.get("seller", {}).get("nickname") if isinstance(order.get("seller"), dict) else None,
                )
                db.add(nova)
                db.flush()
                detail = SaidaDetail(
                    id_saida=nova.id_saida,
                    id_entregador=None,
                    status="Aguardando coleta",
                    **{k: v for k, v in dest_fields.items() if v is not None},
                )
                db.add(detail)
                db.add(
                    SaidaHistorico(
                        id_saida=nova.id_saida,
                        evento="ml_int_sync",
                        status_novo="aguardando_coleta",
                    )
                )
                db.commit()
                created += 1
            except Exception as e:
                db.rollback()
                errors.append({"shipment_id": sid, "error": str(e)})
    return {"created": created, "errors": errors}
