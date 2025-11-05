from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import requests

from db import get_db
from ml_token_service import get_valid_ml_access_token

router = APIRouter(prefix="/ml", tags=["Mercado Livre"])

@router.get("/me")
def ml_me(db: Session = Depends(get_db)):
    try:
        access_token = get_valid_ml_access_token(db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get("https://api.mercadolibre.com/users/me", headers=headers)

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.json())

    return resp.json()
