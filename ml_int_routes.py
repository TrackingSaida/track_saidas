# ml_int_routes.py - Rotas ML Int (OAuth, sellers, envios, volume, etiqueta)
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from sqlalchemy import or_, select

from auth import get_current_user
from db import get_db
from models import MLConexao, Owner, Saida, SaidaDetail, SaidaHistorico, User
from ml_int_envios_pure import destinatario_to_saida_detail_fields, ml_shipment_destinatario
from ml_int_service import (
    exchange_code_for_token,
    fetch_orders_search,
    fetch_shipment,
    fetch_shipments_by_tracking,
    get_me,
    get_valid_access_token,
    listar_envios_flex_pendentes,
    ml_conexao_status,
)


def _normalize_sub_base(value: str | None) -> str:
    return (value or "").strip()


def _upsert_ml_conexao(
    db: Session,
    *,
    user_id_ml: int,
    sub_base_value: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime,
    nickname: str,
) -> MLConexao:
    """
    Atualiza a conexão mais recente do par (user_id_ml, sub_base) e remove duplicatas.
    Evita múltiplas linhas do mesmo seller na mesma sub_base.
    """
    existentes = (
        db.query(MLConexao)
        .filter(
            MLConexao.user_id_ml == user_id_ml,
            MLConexao.sub_base == sub_base_value,
        )
        .order_by(MLConexao.criado_em.desc(), MLConexao.id.desc())
        .all()
    )
    if existentes:
        principal = existentes[0]
        principal.access_token = access_token
        if refresh_token:
            principal.refresh_token = refresh_token
        principal.expires_at = expires_at
        principal.atualizado_em = datetime.utcnow()
        if nickname:
            principal.user_nickname_ml = nickname
        for dup in existentes[1:]:
            db.delete(dup)
        db.commit()
        db.refresh(principal)
        return principal

    novo = MLConexao(
        sub_base=sub_base_value,
        user_id_ml=user_id_ml,
        user_nickname_ml=nickname or None,
        access_token=access_token,
        refresh_token=refresh_token or "",
        expires_at=expires_at,
    )
    db.add(novo)
    db.commit()
    db.refresh(novo)
    return novo

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
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri}
    if state:
        q["state"] = state
    params = urlencode(q)
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

    base_url = (FRONTEND_AFTER_CALLBACK or "").rstrip("/")
    success_page = f"{base_url}/autenticacao-sucesso.html"

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    sub_base_value = _normalize_sub_base(state)
    nickname = (user_me.get("nickname") or "").strip()

    # Nome fantasia do owner para exibir na página de sucesso (prioridade sobre sub_base)
    nome_fantasia = None
    if sub_base_value:
        owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base_value))
        if owner:
            nome_fantasia = (getattr(owner, "nome_fantasia", None) or "").strip() or None

    def success_url() -> str:
        params = ["ml=ok"]
        if sub_base_value:
            params.append("sub_base=" + quote(sub_base_value, safe=""))
        if nome_fantasia:
            params.append("nome_fantasia=" + quote(nome_fantasia, safe=""))
        if nickname:
            params.append("seller_name=" + quote(nickname, safe=""))
        params.append("v=2")
        params.append("_t=" + str(int(time.time())))
        return success_page + "?" + "&".join(params)

    _upsert_ml_conexao(
        db,
        user_id_ml=user_id_ml,
        sub_base_value=sub_base_value,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        nickname=nickname,
    )
    return RedirectResponse(url=success_url())


# ---------- Sellers (auth, filtro por sub_base) ----------
@router.get("/sellers")
def ml_int_sellers(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista conexões (sellers) da sub_base do usuário (1 linha por user_id_ml)."""
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito a root e admin.")
    sub_base = getattr(user, "sub_base", None)
    q = db.query(MLConexao).filter(MLConexao.sub_base == sub_base)
    conexoes = q.order_by(MLConexao.criado_em.desc(), MLConexao.id.desc()).all()
    # Deduplica por seller: mantém a conexão mais recente
    vistos: set[int] = set()
    unicos: list[MLConexao] = []
    for c in conexoes:
        if c.user_id_ml in vistos:
            continue
        vistos.add(c.user_id_ml)
        unicos.append(c)
    return [
        {
            "id": c.id,
            "user_id_ml": c.user_id_ml,
            "user_nickname_ml": c.user_nickname_ml,
            "sub_base": c.sub_base,
            "status": ml_conexao_status(c),
            "criado_em": c.criado_em.isoformat() if c.criado_em else None,
        }
        for c in unicos
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
            db=db,
            sub_base=sub_base,
        )
    except requests.HTTPError as e:
        raise HTTPException(e.response.status_code, e.response.text or "Erro na API ML")
    results = data.get("results") or []
    total = data.get("paging", {}).get("total", len(results))
    return {"total": total, "results": results, "seller_id": user_id_ml}


# ---------- Vendas do seller (histórico via /orders/search) ----------
@router.get("/sellers/{user_id_ml:int}/vendas")
def ml_int_vendas_seller(
    user_id_ml: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    order_status: str | None = Query(
        None,
        description="Filtro de status (ex.: paid, shipped, delivered). Se vazio, traz todos.",
    ),
    date_from: str | None = Query(
        None,
        description="Data inicial de criação do pedido (ISO 8601, ex.: 2026-03-01T00:00:00).",
    ),
    date_to: str | None = Query(
        None,
        description="Data final de criação do pedido (ISO 8601).",
    ),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, le=100),
):
    """
    Lista vendas do seller diretamente do /orders/search do Mercado Livre.
    Permite filtrar por status e intervalo de datas.
    """
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    try:
        token = get_valid_access_token(db, user_id_ml, sub_base)
    except (LookupError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    try:
        data = fetch_orders_search(
            token,
            user_id_ml,
            order_status=order_status,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=limit,
            db=db,
            sub_base=sub_base,
        )
    except requests.HTTPError as e:
        raise HTTPException(e.response.status_code, e.response.text or "Erro na API ML")
    return {
        "seller_id": user_id_ml,
        "paging": data.get("paging"),
        "results": data.get("results") or [],
    }


# ---------- Envios do seller (tracking + destination) ----------
@router.get("/sellers/{user_id_ml:int}/envios")
def ml_int_envios_seller(
    user_id_ml: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    dias: int = Query(7, ge=1, le=30, description="Janela de vendas (dias) a considerar."),
    max_pedidos: int = Query(300, ge=50, le=1000),
):
    """
    Prévia (somente leitura) dos envios Flex do seller prontos para coleta,
    com destinatário normalizado e indicação se já existem no sistema.
    """
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    data = _listar_pendentes_ou_http(db, user_id_ml, sub_base, dias=dias, max_pedidos=max_pedidos)
    ja_registrados = _shipments_ja_registrados(db, sub_base, data["envios"])
    for envio in data["envios"]:
        envio["ja_registrado"] = envio["shipment_id"] in ja_registrados
    return data


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
            ship = fetch_shipment(
                token, shipment_id, db=db, user_id_ml=c.user_id_ml, sub_base=sub_base,
            )
            dest = ship.get("destination") or {}
            return {
                "shipment_id": shipment_id,
                "tracking_number": ship.get("tracking_number"),
                "status": ship.get("status"),
                "substatus": ship.get("substatus"),
                "destination": dest,
                "destinatario": ml_shipment_destinatario(ship),
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
            data = fetch_shipments_by_tracking(
                token, tracking_number, db=db, user_id_ml=c.user_id_ml, sub_base=sub_base,
            )
            results = data.get("results") or []
            if not results:
                continue
            sid = results[0].get("id")
            if sid:
                ship = fetch_shipment(
                    token, int(sid), db=db, user_id_ml=c.user_id_ml, sub_base=sub_base,
                )
                dest = ship.get("destination") or {}
                return {
                    "shipment_id": sid,
                    "tracking_number": ship.get("tracking_number"),
                    "status": ship.get("status"),
                    "substatus": ship.get("substatus"),
                    "destination": dest,
                    "destinatario": ml_shipment_destinatario(ship),
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
            ship = fetch_shipment(
                token, shipment_id, db=db, user_id_ml=c.user_id_ml, sub_base=sub_base,
            )
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


# ---------- Sync: envios Flex pendentes de coleta → saidas (aguardando_coleta) ----------
def _listar_pendentes_ou_http(
    db: Session, user_id_ml: int, sub_base: str | None, *, dias: int, max_pedidos: int,
) -> dict:
    try:
        return listar_envios_flex_pendentes(
            db, user_id_ml, sub_base or "", dias=dias, max_pedidos=max_pedidos,
        )
    except (LookupError, RuntimeError) as e:
        raise HTTPException(404, str(e))
    except requests.HTTPError as e:
        raise HTTPException(e.response.status_code, e.response.text or "Erro na API ML")


def _shipments_ja_registrados(db: Session, sub_base: str | None, envios: list[dict]) -> set[int]:
    """shipment_ids já existentes na sub_base (por ml_shipment_id ou código lido no scanner)."""
    if not envios:
        return set()
    sids = [e["shipment_id"] for e in envios]
    por_codigo = {e["codigo"]: e["shipment_id"] for e in envios}
    rows = db.execute(
        select(Saida.ml_shipment_id, Saida.codigo).where(
            Saida.sub_base == sub_base,
            or_(Saida.ml_shipment_id.in_(sids), Saida.codigo.in_(list(por_codigo))),
        )
    ).all()
    encontrados: set[int] = set()
    for ml_sid, codigo in rows:
        if ml_sid in sids:
            encontrados.add(ml_sid)
        if codigo in por_codigo:
            encontrados.add(por_codigo[codigo])
    return encontrados


@router.post("/sync-envios")
def ml_int_sync_envios(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    user_id_ml: int | None = Query(None, description="Sincroniza só este seller; vazio = todos da sub_base."),
    dias: int = Query(7, ge=1, le=30),
    max_pedidos: int = Query(300, ge=50, le=1000),
):
    """
    Para cada seller conectado à sub_base, busca envios Flex prontos para coleta
    ainda não registrados e cria Saida (aguardando_coleta) + SaidaDetail (destinatário).
    """
    if user.role not in (0, 1):
        raise HTTPException(403, "Acesso restrito.")
    sub_base = getattr(user, "sub_base", None)
    if not sub_base:
        raise HTTPException(400, "Usuário sem sub_base.")
    q = db.query(MLConexao).filter(MLConexao.sub_base == sub_base)
    if user_id_ml is not None:
        q = q.filter(MLConexao.user_id_ml == user_id_ml)
    conexoes = q.order_by(MLConexao.criado_em.desc(), MLConexao.id.desc()).all()
    if user_id_ml is not None and not conexoes:
        raise HTTPException(404, "Seller não conectado a esta sub_base.")

    vistos: set[int] = set()
    sellers = []
    total_criados = 0
    for conexao in conexoes:
        if conexao.user_id_ml in vistos:
            continue
        vistos.add(conexao.user_id_ml)
        seller = {
            "user_id_ml": conexao.user_id_ml,
            "nickname": conexao.user_nickname_ml,
            "pendentes": 0,
            "criados": 0,
            "ja_registrados": 0,
            "erros": [],
        }
        sellers.append(seller)
        try:
            data = listar_envios_flex_pendentes(
                db, conexao.user_id_ml, sub_base, dias=dias, max_pedidos=max_pedidos,
            )
        except (LookupError, RuntimeError) as e:
            seller["erros"].append({"erro": str(e)})
            continue
        except requests.HTTPError as e:
            seller["erros"].append({"erro": f"HTTP {e.response.status_code}: {(e.response.text or '')[:200]}"})
            continue
        seller["erros"].extend(data["erros"])
        envios = data["envios"]
        seller["pendentes"] = len(envios)
        ja_registrados = _shipments_ja_registrados(db, sub_base, envios)
        for envio in envios:
            sid = envio["shipment_id"]
            if sid in ja_registrados:
                seller["ja_registrados"] += 1
                continue
            order_ids = envio.get("order_ids") or []
            try:
                nova = Saida(
                    sub_base=sub_base,
                    codigo=envio["codigo"],
                    servico="Mercado Livre",
                    status="aguardando_coleta",
                    ml_shipment_id=sid,
                    ml_order_id=order_ids[0] if order_ids else None,
                    base=conexao.user_nickname_ml,
                )
                db.add(nova)
                db.flush()
                db.add(
                    SaidaDetail(
                        id_saida=nova.id_saida,
                        id_entregador=None,
                        status="Aguardando coleta",
                        **destinatario_to_saida_detail_fields(envio["destinatario"]),
                    )
                )
                db.add(
                    SaidaHistorico(
                        id_saida=nova.id_saida,
                        evento="ml_int_sync",
                        status_novo="aguardando_coleta",
                        user_id=getattr(user, "id", None),
                    )
                )
                db.commit()
                seller["criados"] += 1
                total_criados += 1
            except Exception as e:
                db.rollback()
                seller["erros"].append({"shipment_id": sid, "erro": str(e)[:200]})
    return {"criados": total_criados, "dias": dias, "sellers": sellers}
