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
from ml_token_service import get_valid_ml_access_token
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
# 1) Gera o link para o cliente autorizar
# ============================================================
@router.get("/connect")
def ml_connect():
    """
    Devolve a URL de autorização do Mercado Livre.
    Você manda isso para o cliente.
    """
    if not ML_CLIENT_ID or not ML_REDIRECT_URI:
        raise HTTPException(500, "ML_CLIENT_ID ou ML_REDIRECT_URI não configurados.")

    auth_url = (
        f"{ML_AUTH_BASE}"
        f"?response_type=code"
        f"&client_id={ML_CLIENT_ID}"
        f"&redirect_uri={ML_REDIRECT_URI}"
    )
    return {"auth_url": auth_url}


# ============================================================
# 2) Callback chamado pelo Mercado Livre após o aceite
#    - recebe ?code=...
#    - troca por tokens
#    - salva SOMENTE SE for um novo user_id_ml
#    - redireciona o usuário para o seu site
# ============================================================
@router.get("/callback")
def ml_callback(code: str, db: Session = Depends(get_db)):
    if not ML_CLIENT_ID or not ML_CLIENT_SECRET or not ML_REDIRECT_URI:
        raise HTTPException(500, "Variáveis do Mercado Livre não configuradas.")

    # 1. troca code por tokens
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
        raise HTTPException(resp.status_code, f"Erro ao obter token no ML: {resp.text}")

    token_data = resp.json()
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)

    # 2. pega dados do usuário no ML
    me_resp = requests.get(
        ML_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if me_resp.status_code != 200:
        raise HTTPException(500, "Token obtido, mas não foi possível ler /users/me")
    me_data = me_resp.json()
    user_id_ml = me_data["id"]

    # 3. verifica se já existe esse user_id_ml
    existente = (
        db.query(MercadoLivreToken)
        .filter(MercadoLivreToken.user_id_ml == user_id_ml)
        .first()
    )

    # se já existir, não faz nada — apenas ignora e redireciona
    if existente:
        final_url = f"{FRONTEND_AFTER_CALLBACK}?ml=ja_existe"
        return RedirectResponse(url=final_url, status_code=302)

    # 4. se não existir, salva novo registro
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    novo = MercadoLivreToken(
        user_id_ml=user_id_ml,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    db.add(novo)
    db.commit()

    # 5. redireciona o usuário para o site
    final_url = f"{FRONTEND_AFTER_CALLBACK}?ml=ok"
    return RedirectResponse(url=final_url, status_code=302)


# ============================================================
# 3) Varredura: obter /users/me de TODAS as contas salvas
# ============================================================
@router.get("/me")
def ml_me(db: Session = Depends(get_db)):
    """
    Faz uma varredura em TODAS as contas do Mercado Livre que temos salvas
    na tabela `mercado_livre_tokens` e tenta chamar /users/me para cada uma.
    Retorna uma lista com o status de cada conta.
    """
    tokens = db.execute(select(MercadoLivreToken)).scalars().all()

    if not tokens:
        raise HTTPException(status_code=404, detail="Nenhum token do Mercado Livre encontrado na tabela.")

    resultados = []

    for tk in tokens:
        headers = {"Authorization": f"Bearer {tk.access_token}"}
        resp = requests.get(ML_ME_URL, headers=headers)

        if resp.status_code == 200:
            resultados.append(
                {
                    "id": tk.id,
                    "user_id_ml": tk.user_id_ml,
                    "status": "ok",
                    "data": resp.json(),
                }
            )
        else:
            # mesmo se der erro, registra
            detail = {}
            try:
                detail = resp.json()
            except Exception:
                detail = {"raw": resp.text}
            resultados.append(
                {
                    "id": tk.id,
                    "user_id_ml": tk.user_id_ml,
                    "status": "erro",
                    "http_status": resp.status_code,
                    "detail": detail,
                }
            )

    return {
        "total_tokens": len(tokens),
        "resultados": resultados,
    }


# ============================================================
# 3.1) Varredura de pedidos (/orders/search?seller=...)
#      usando TODOS os tokens salvos
#      (mantido, mas agora temos também o /orders-by-seller)
# ============================================================
@router.get("/orders-scan")
def ml_orders_scan(db: Session = Depends(get_db)):
    """
    Para cada conta Mercado Livre salva na tabela, tenta listar TODAS as orders
    usando o endpoint:
        GET https://api.mercadolibre.com/orders/search?seller={USER_ID_ML}
    Faz paginação até acabar.
    Os tokens que não tiverem user_id_ml ou que não retornarem pedidos são ignorados,
    mas o resultado vem marcado.
    """
    tokens = db.execute(select(MercadoLivreToken)).scalars().all()
    if not tokens:
        raise HTTPException(status_code=404, detail="Nenhum token do Mercado Livre encontrado na tabela.")

    resultados = []

    for tk in tokens:
        if not tk.user_id_ml:
            resultados.append(
                {
                    "token_id": tk.id,
                    "user_id_ml": None,
                    "status": "sem_user_id_ml",
                    "total_orders": 0,
                    "orders": [],
                }
            )
            continue

        headers = {"Authorization": f"Bearer {tk.access_token}"}

        all_orders = []
        offset = 0
        limit = 50
        erro = None

        while True:
            params = {
                "seller": tk.user_id_ml,
                "offset": offset,
                "limit": limit,
            }

            resp = requests.get(ML_ORDERS_SEARCH_URL, headers=headers, params=params)

            if resp.status_code != 200:
                # para esse token, registramos o erro e saímos do loop
                try:
                    detail = resp.json()
                except Exception:
                    detail = {"raw": resp.text}
                erro = {
                    "http_status": resp.status_code,
                    "detail": detail,
                }
                break

            data = resp.json()
            batch_orders = data.get("results") or []
            paging = data.get("paging") or {}

            all_orders.extend(batch_orders)

            total = paging.get("total", 0)
            if len(all_orders) >= total or len(batch_orders) < limit:
                break

            offset += limit

        if erro:
            resultados.append(
                {
                    "token_id": tk.id,
                    "user_id_ml": tk.user_id_ml,
                    "status": "erro",
                    "erro": erro,
                    "total_orders": len(all_orders),
                    "orders": all_orders,
                }
            )
        else:
            resultados.append(
                {
                    "token_id": tk.id,
                    "user_id_ml": tk.user_id_ml,
                    "status": "ok",
                    "total_orders": len(all_orders),
                    "orders": all_orders,
                }
            )

    return {
        "total_tokens": len(tokens),
        "resultados": resultados,
    }


# ============================================================
# 3.2) Consulta direta por seller_id (sem varredura)
#      aqui você informa o seller_id e opcionalmente from/to
# ============================================================
@router.get("/orders-by-seller")
def ml_orders_by_seller(
    seller_id: int = Query(..., description="ID do vendedor no Mercado Livre"),
    from_date: str = Query(None, description="Data inicial ISO8601, ex: 2025-01-01T00:00:00.000-00:00"),
    to_date: str = Query(None, description="Data final ISO8601, ex: 2025-01-31T23:59:59.000-00:00"),
    db: Session = Depends(get_db),
):
    """
    Consulta as orders de UM vendedor específico, usando o token mais recente da tabela.
    Isso elimina o fator 'varredura'.
    """
    # pega um token válido (o mais recente) para autenticar
    try:
        access_token = get_valid_ml_access_token(db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    headers = {"Authorization": f"Bearer {access_token}"}

    params = {
        "seller": seller_id,
        "offset": 0,
        "limit": 50,
    }

    if from_date:
        params["order.date_created.from"] = from_date
    if to_date:
        params["order.date_created.to"] = to_date

    resp = requests.get(ML_ORDERS_SEARCH_URL, headers=headers, params=params)

    if resp.status_code != 200:
        # devolve o erro cru do ML pra gente ver o que está rolando
        try:
            detail = resp.json()
        except Exception:
            detail = {"raw": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


# ============================================================
# 3.3) Ver permissões / "o que o token deixa"
#      aqui olhamos o token específico que está salvo no seu banco
# ============================================================
@router.get("/token-permissions/{token_id}")
def ml_token_permissions(token_id: int, db: Session = Depends(get_db)):
    """
    Consulta quais dados o token consegue acessar AGORA.
    Faz:
      1) /users/me com esse token
      2) tenta /applications/{ML_CLIENT_ID} pra ver dados do app (se o ML permitir)
    Isso ajuda a saber se o token ainda está vivo e se pertence ao seller certo.
    """
    tk = db.get(MercadoLivreToken, token_id)
    if not tk:
        raise HTTPException(404, "Token não encontrado.")

    headers = {"Authorization": f"Bearer {tk.access_token}"}

    # 1) quem sou eu com esse token?
    me_resp = requests.get(ML_ME_URL, headers=headers)
    me_data = None
    if me_resp.status_code == 200:
        me_data = me_resp.json()
    else:
        try:
            me_data = me_resp.json()
        except Exception:
            me_data = {"raw": me_resp.text}

    # 2) tenta ver info da aplicação (não é exatamente "escopos do token", mas ajuda)
    app_data = None
    if ML_CLIENT_ID:
        app_resp = requests.get(f"https://api.mercadolibre.com/applications/{ML_CLIENT_ID}")
        if app_resp.status_code == 200:
            app_data = app_resp.json()
        else:
            try:
                app_data = app_resp.json()
            except Exception:
                app_data = {"raw": app_resp.text}

    return {
        "token_id": token_id,
        "user_id_ml_salvo": tk.user_id_ml,
        "users_me": me_data,
        "application_info": app_data,
    }


# ============================================================
# 4) Consulta envio por tracking (mantido)
# ============================================================
@router.get("/shipment-by-tracking")
def ml_shipment_by_tracking(
    tracking_number: str = Query(..., description="Código de rastreio da encomenda"),
    db: Session = Depends(get_db),
):
    """
    Consulta um envio no Mercado Livre usando APENAS o código de rastreio.
    Usa o endpoint: GET https://api.mercadolibre.com/shipments/search?tracking_number=...
    Se encontrar, devolve o endereço do destinatário (receiver_address).
    """
    # pega token válido (o mais recente da tabela)
    try:
        access_token = get_valid_ml_access_token(db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    headers = {"Authorization": f"Bearer {access_token}"}

    url = "https://api.mercadolibre.com/shipments/search"
    params = {"tracking_number": tracking_number}

    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = {"raw": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=detail)

    data = resp.json()
    results = data.get("results") or []
    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum envio encontrado para o código de rastreio '{tracking_number}'.",
        )

    shipment = results[0]

    receiver_address = shipment.get("receiver_address")
    if not receiver_address:
        return {
            "tracking_number": tracking_number,
            "shipment_id": shipment.get("id"),
            "message": "Envio encontrado, mas não há receiver_address nos dados retornados.",
            "raw": shipment,
        }

    return {
        "tracking_number": tracking_number,
        "shipment_id": shipment.get("id"),
        "status": shipment.get("status"),
        "substatus": shipment.get("substatus"),
        "receiver_address": receiver_address,
    }
