# trechos novos para ml_routes.py
import os
from datetime import datetime, timedelta, timezone
import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from db import get_db
from models import MercadoLivreToken

router = APIRouter(prefix="/ml", tags=["Mercado Livre"])

ML_CLIENT_ID = os.getenv("ML_CLIENT_ID")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET")
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI")  # precisa estar igual ao cadastrado no ML
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_ME_URL = "https://api.mercadolibre.com/users/me"


@router.get("/connect")
def ml_connect():
    """
    Devolve a URL que você pode mandar para o cliente autorizar o app no Mercado Livre.
    (Você também pode simplesmente montar isso no front.)
    """
    if not ML_CLIENT_ID or not ML_REDIRECT_URI:
        raise HTTPException(500, "ML_CLIENT_ID ou ML_REDIRECT_URI não configurados")

    auth_url = (
        "https://auth.mercadolibre.com.br/authorization"
        f"?response_type=code&client_id={ML_CLIENT_ID}&redirect_uri={ML_REDIRECT_URI}"
    )
    return {"auth_url": auth_url}


@router.get("/callback")
def ml_callback(code: str, db: Session = Depends(get_db)):
    """
    Endpoint que o Mercado Livre chama depois que o cliente aceita.
    Aqui trocamos o code por tokens e salvamos na tabela mercado_livre_tokens.
    """
    if not ML_CLIENT_ID or not ML_CLIENT_SECRET or not ML_REDIRECT_URI:
        raise HTTPException(500, "Variáveis do Mercado Livre não configuradas.")

    # 1. trocar code por tokens
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

    # 2. opcional mas bom: pegar quem é o usuário do ML (id ML)
    me_resp = requests.get(
        ML_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if me_resp.status_code != 200:
        raise HTTPException(500, "Token obtido, mas não deu para ler /users/me")

    me_data = me_resp.json()
    user_id_ml = me_data["id"]

    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    # 3. salvar no banco como NOVA LINHA
    novo = MercadoLivreToken(
        user_id_ml=user_id_ml,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )
    db.add(novo)
    db.commit()
    db.refresh(novo)

    # você pode redirecionar pra uma página sua, mas vou só devolver json
    return {
        "detail": "Conta do Mercado Livre conectada e salva.",
        "user_id_ml": user_id_ml,
        "expires_at": expires_at.isoformat(),
    }
