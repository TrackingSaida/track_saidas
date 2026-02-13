# ============================================
# PUBLIC SIGNUP — Criação de Owner + Usuário Inicial (role=1)
# ============================================

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
import unicodedata
import re

from db import get_db
from auth import get_password_hash
from models import Owner, User

router = APIRouter(prefix="/public", tags=["Public Signup"])


# ---------- Helpers ----------
def normalize(name: str) -> str:
    """Remove acentos, normaliza espaços e deixa minúsculo."""
    if not name:
        return ""

    # Remove acentos
    nfkd = unicodedata.normalize("NFKD", name)
    no_accent = "".join([c for c in nfkd if not unicodedata.combining(c)])

    # Normalizar espaços
    no_spacing = re.sub(r"\s+", " ", no_accent)

    return no_spacing.strip().lower()


# ---------- Schema ----------
class PublicSignupPayload(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3)
    password: str = Field(min_length=8)
    nome: str
    sobrenome: str
    contato: str = Field(min_length=8)

    sub_base: str = Field(min_length=2, description="Nome da sub-base (ex: DG EXPRESS)")

    model_config = {"from_attributes": True}


# ---------- Endpoint ----------
@router.post("/signup", status_code=status.HTTP_201_CREATED)
def public_signup(
    body: PublicSignupPayload,
    db: Session = Depends(get_db)
):

    # ----------------------------------------
    # NORMALIZAÇÃO DE SUB_BASE
    # ----------------------------------------
    sub_norm = normalize(body.sub_base)

    all_owners = db.scalars(select(Owner)).all()
    for ow in all_owners:
        if normalize(ow.sub_base) == sub_norm:
            raise HTTPException(
                status_code=409,
                detail=f"Já existe um Owner para '{ow.sub_base}'."
            )

    # ----------------------------------------
    # VALIDAÇÃO DE EMAIL E USERNAME
    # ----------------------------------------
    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="Email já cadastrado.")

    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="Username já cadastrado.")

    # ----------------------------------------
    # NORMALIZAR SUB_BASE FINAL
    # (Preserva o texto original, mas evita problemas)
    # ----------------------------------------
    sub_base_final = body.sub_base.strip()

    # ----------------------------------------
    # CRIAR OWNER
    # ----------------------------------------
    owner = Owner(
        email=body.email,
        username=body.username,
        valor=0.0,
        sub_base=sub_base_final,
        contato=body.contato,
        ativo=True,
        ignorar_coleta=False
    )

    db.add(owner)
    db.commit()
    db.refresh(owner)

    # ----------------------------------------
    # CRIAR USUÁRIO ADMIN INICIAL (role=1)
    # ----------------------------------------
    user = User(
        email=body.email,
        username=body.username,
        password_hash=get_password_hash(body.password),
        contato=body.contato,
        nome=body.nome,
        sobrenome=body.sobrenome,
        status=True,
        role=1,         # admin
        coletador=False,
        sub_base=sub_base_final
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    # ----------------------------------------
    # RETORNO
    # ----------------------------------------
    return {
        "ok": True,
        "message": "Conta criada com sucesso.",
        "owner_id": owner.id_owner,
        "user_id": user.id,
        "sub_base": sub_base_final
    }
