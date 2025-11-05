# ml_token_service.py
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models import MercadoLivreToken

ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"


def get_latest_ml_token(db: Session) -> Optional[MercadoLivreToken]:
    """
    Pega o último token salvo no banco (o mais recente).
    Continua útil para endpoints que usam "1 token padrão".
    """
    return (
        db.query(MercadoLivreToken)
        .order_by(MercadoLivreToken.id.desc())
        .first()
    )


def refresh_ml_token(db: Session, ml_token: MercadoLivreToken) -> MercadoLivreToken:
    """
    Usa o refresh_token salvo para pegar um novo access_token no Mercado Livre.
    Atualiza a linha existente.
    """
    data = {
        "grant_type": "refresh_token",
        "client_id": os.getenv("ML_CLIENT_ID"),
        "client_secret": os.getenv("ML_CLIENT_SECRET"),
        "refresh_token": ml_token.refresh_token,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(ML_TOKEN_URL, data=data, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Erro ao renovar token do Mercado Livre: {resp.text}")

    new_tokens = resp.json()

    ml_token.access_token = new_tokens["access_token"]
    ml_token.refresh_token = new_tokens.get("refresh_token", ml_token.refresh_token)
    ml_token.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens["expires_in"])

    db.commit()
    db.refresh(ml_token)
    return ml_token


def get_valid_ml_access_token(db: Session) -> str:
    """
    Retorna SEMPRE um access_token válido.
    - se o que está no banco ainda não expirou: devolve
    - se expirou: faz refresh, salva e devolve
    """
    ml_token = get_latest_ml_token(db)
    if not ml_token:
        raise RuntimeError("Nenhum token do Mercado Livre foi encontrado no banco.")

    if ml_token.expires_at and ml_token.expires_at > datetime.utcnow():
        return ml_token.access_token

    ml_token = refresh_ml_token(db, ml_token)
    return ml_token.access_token
