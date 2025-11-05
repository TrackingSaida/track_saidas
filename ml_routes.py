# ml_routes.py
from __future__ import annotations

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from db import get_db
from ml_token_service import get_valid_ml_access_token  # continua sendo usado no outro endpoint
from models import MercadoLivreToken  # <<< importa o model da tabela mercado_livre_tokens

router = APIRouter(prefix="/ml", tags=["Mercado Livre"])


@router.get("/me")
def ml_me(db: Session = Depends(get_db)):
    """
    Faz uma varredura em TODAS as contas do Mercado Livre que temos salvas
    na tabela `mercado_livre_tokens` e tenta chamar /users/me para cada uma.
    Retorna uma lista com o status de cada conta.
    """
    # 1. buscar todos os tokens salvos
    tokens = db.execute(select(MercadoLivreToken)).scalars().all()

    if not tokens:
        raise HTTPException(status_code=404, detail="Nenhum token do Mercado Livre encontrado na tabela.")

    resultados = []

    for tk in tokens:
        headers = {"Authorization": f"Bearer {tk.access_token}"}
        resp = requests.get("https://api.mercadolibre.com/users/me", headers=headers)

        if resp.status_code == 200:
            resultados.append(
                {
                    "id": tk.id,                  # id da sua tabela
                    "user_id_ml": tk.user_id_ml,  # id do usuário no ML que você já está guardando
                    "status": "ok",
                    "data": resp.json(),
                }
            )
        else:
            # não derruba o endpoint inteiro, só marca o erro daquele token
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
    # 1. pega token válido (renova se precisar) -- aqui continua do jeito que já estava
    try:
        access_token = get_valid_ml_access_token(db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    headers = {"Authorization": f"Bearer {access_token}"}

    # 2. chama o ML filtrando pelo código de rastreio
    url = "https://api.mercadolibre.com/shipments/search"
    params = {"tracking_number": tracking_number}

    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code != 200:
        # erro vindo do próprio ML
        raise HTTPException(status_code=resp.status_code, detail=resp.json())

    data = resp.json()

    # 3. checa se achou algum envio
    results = data.get("results") or []
    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"Nenhum envio encontrado para o código de rastreio '{tracking_number}'.",
        )

    # normalmente vem 1 resultado; vamos pegar o primeiro
    shipment = results[0]

    receiver_address = shipment.get("receiver_address")
    if not receiver_address:
        # achou o envio, mas não veio endereço (pode acontecer dependendo do tipo de envio)
        return {
            "tracking_number": tracking_number,
            "shipment_id": shipment.get("id"),
            "message": "Envio encontrado, mas não há receiver_address nos dados retornados.",
            "raw": shipment,
        }

    # 4. devolve só o endereço (e alguns dados úteis juntos)
    return {
        "tracking_number": tracking_number,
        "shipment_id": shipment.get("id"),
        "status": shipment.get("status"),
        "substatus": shipment.get("substatus"),
        "receiver_address": receiver_address,
    }
