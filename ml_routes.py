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
            resultados.append(
                {
                    "id": tk.id,
                    "user_id_ml": tk.user_id_ml,
                    "status": "erro",
                    "http_status": resp.status_code,
                    "detail": resp.json(),
                }
            )

    return {
        "total_tokens": len(tokens),
        "resultados": resultados,
    }


# ============================================================
# 3.1) Varredura de pedidos (/orders/search?seller=...)
#      usando TODOS os tokens salvos
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
        # se por algum motivo o token não tiver user_id_ml salvo, só pula
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
        limit = 50  # limite padrão do ML
        erro = None

        while True:
            params = {
                "seller": tk.user_id_ml,
                "offset": offset,
                "limit": limit,
            }

            resp = requests.get(ML_ORDERS_SEARCH_URL, headers=headers, params=params)

            # se deu erro (token expirado, 401, 403, etc.), para esse usuário e registra
            if resp.status_code != 200:
                erro = {
                    "http_status": resp.status_code,
                    "detail": resp.json() if resp.content else {},
                }
                break

            data = resp.json()
            batch_orders = data.get("results") or []
            paging = data.get("paging") or {}

            all_orders.extend(batch_orders)

            total = paging.get("total", 0)
            # se já chegamos no total ou o que voltou foi menor que o limite, paramos
            if len(all_orders) >= total or len(batch_orders) < limit:
                break

            # senão segue para próxima página
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
                    # IMPORTANTE: aqui já vem o campo shipping.id em cada order
                    # que vamos usar no próximo passo para consultar o endereço.
                    "orders": all_orders,
                }
            )

    return {
        "total_tokens": len(tokens),
        "resultados": resultados,
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
        raise HTTPException(status_code=resp.status_code, detail=resp.json())

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
