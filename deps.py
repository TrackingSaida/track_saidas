# deps.py
import os, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session
from db import SessionLocal
from models import User

JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
JWT_ALG = "HS256"
auth_scheme = HTTPBearer(auto_error=True)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(credentials=Depends(auth_scheme), db: Session = Depends(get_db)) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token sem sub")

    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")

    # papel = tipo_de_cadastro (1,2,3); default seguro = 3
    user.role = int(getattr(user, "tipo_de_cadastro", 3) or 3)
    return user
