# ml_token_service.py
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional, List

import requests
from sqlalchemy.orm import Session

from models import MercadoLivreToken

ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"


# ============================================================
# Funções utilitárias de token
# ============================================================

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


def refresh_ml_token(db: Session, ml_token: MercadoLivreToken) -> Optional[MercadoLivreToken]:
    """
    Usa o refresh_token salvo para pegar um novo access_token no Mercado Livre.
    Atualiza a linha existente.
    Retorna None se não conseguir renovar.
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
        # falhou, retorna None pra que quem chamou saiba
        return None

    new_tokens = resp.json()

    ml_token.access_token = new_tokens["access_token"]
    ml_token.refresh_token = new_tokens.get("refresh_token", ml_token.refresh_token)
    ml_token.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens["expires_in"])

    db.commit()
    db.refresh(ml_token)
    print(f"[ML] Token renovado para user_id_ml={ml_token.user_id_ml}")
    return ml_token


def get_valid_ml_access_token(db: Session) -> str:
    """
    Retorna SEMPRE um access_token válido.
    - se o que está no banco ainda não expirou: devolve
    - se expirou: tenta refresh, salva e devolve
    - se não conseguir renovar: levanta exceção
    """
    ml_token = get_latest_ml_token(db)
    if not ml_token:
        raise RuntimeError("Nenhum token do Mercado Livre foi encontrado no banco.")

    if ml_token.expires_at and ml_token.expires_at > datetime.utcnow():
        return ml_token.access_token

    ml_token = refresh_ml_token(db, ml_token)
    if not ml_token:
        raise RuntimeError("Não foi possível renovar o token do Mercado Livre.")
    return ml_token.access_token


# ============================================================
# Renovação em massa (usado no startup da API)
# ============================================================

def refresh_all_ml_tokens(db: Session) -> None:
    """
    Varre TODAS as linhas de mercado_livre_tokens.
    - se o token estiver válido, não faz nada
    - se estiver vencido, tenta renovar
    - ignora erros individuais (para não travar a inicialização)
    """
    tokens: List[MercadoLivreToken] = (
        db.query(MercadoLivreToken)
        .order_by(MercadoLivreToken.id.desc())
        .all()
    )

    if not tokens:
        print("[ML] Nenhum token encontrado para renovar.")
        return

    now = datetime.utcnow()
    print(f"[ML] Iniciando varredura de {len(tokens)} tokens...")

    for tk in tokens:
        # se ainda está válido, pula
        if tk.expires_at and tk.expires_at > now:
            continue

        refreshed = refresh_ml_token(db, tk)
        if not refreshed:
            print(f"[ML] Falha ao renovar token do user_id_ml={tk.user_id_ml}")
            continue

    print("[ML] Varredura concluída.")
