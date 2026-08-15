# ============================================
# PUBLIC SIGNUP — Criação de Owner + Usuário Inicial (role=1)
# ============================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
import unicodedata
import re

from db import get_db
from name_normalizer import normalize_person_name
from auth import get_password_hash
from models import Owner, User, BaseSellerDados

router = APIRouter(prefix="/public", tags=["Public Signup"])


# ---------- Helpers ----------
def normalize(name: str) -> str:
    """Remove acentos, normaliza espaços e deixa minúsculo."""
    if not name:
        return ""

    nfkd = unicodedata.normalize("NFKD", name)
    no_accent = "".join([c for c in nfkd if not unicodedata.combining(c)])
    no_spacing = re.sub(r"\s+", " ", no_accent)

    return no_spacing.strip().lower()


def digits_only(value: Optional[str]) -> str:
    return re.sub(r"\D+", "", value or "")


def present(value: Optional[str]) -> str:
    return (value or "").strip()


# ---------- Schema ----------
class PublicSignupPayload(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3)
    password: str = Field(min_length=8)
    nome: str
    sobrenome: str
    contato: str = Field(min_length=8)

    # Compatível com clientes antigos; se vazio, deriva de nome_fantasia
    sub_base: str = Field(default="", description="Identificador da operação (legado / derivado)")

    # Campos de empresa (opcionais na API; o wizard novo envia todos)
    nome_fantasia: Optional[str] = None
    razao_social: Optional[str] = None
    cnpj: Optional[str] = None
    telefone_empresa: Optional[str] = None
    cep: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- Endpoint ----------
@router.post("/signup", status_code=status.HTTP_201_CREATED)
def public_signup(
    body: PublicSignupPayload,
    db: Session = Depends(get_db)
):
    fantasia = present(body.nome_fantasia)
    razao = present(body.razao_social)
    sub_base_final = present(body.sub_base) or fantasia

    if len(sub_base_final) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Informe o nome fantasia da operação.",
        )

    sub_norm = normalize(sub_base_final)

    all_owners = db.scalars(select(Owner)).all()
    for ow in all_owners:
        if normalize(ow.sub_base or "") == sub_norm:
            raise HTTPException(
                status_code=409,
                detail="Já existe uma operação com este nome fantasia.",
            )

    if db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="Email já cadastrado.")

    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="Username já cadastrado.")

    cnpj_digits = digits_only(body.cnpj)
    has_company_block = any([
        fantasia,
        razao,
        cnpj_digits,
        present(body.telefone_empresa),
        present(body.cep),
        present(body.rua),
        present(body.numero),
        present(body.bairro),
        present(body.cidade),
        present(body.estado),
    ])

    if has_company_block:
        missing = []
        if not fantasia:
            missing.append("nome fantasia")
        if not razao:
            missing.append("razão social")
        if len(cnpj_digits) != 14:
            missing.append("CNPJ válido")
        if len(digits_only(body.telefone_empresa)) < 8:
            missing.append("telefone comercial")
        if len(digits_only(body.cep)) != 8:
            missing.append("CEP")
        if not present(body.rua):
            missing.append("rua")
        if not present(body.numero):
            missing.append("número")
        if not present(body.bairro):
            missing.append("bairro")
        if not present(body.cidade):
            missing.append("cidade")
        if not present(body.estado):
            missing.append("UF")
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Dados da empresa incompletos: " + ", ".join(missing) + ".",
            )

        sellers = db.scalars(select(BaseSellerDados)).all()
        if any(digits_only(s.cnpj) == cnpj_digits for s in sellers):
            raise HTTPException(status_code=409, detail="CNPJ já cadastrado.")

    owner_contato = present(body.telefone_empresa) or present(body.contato)
    nome_fantasia_db = razao or None  # razão social → owner.nome_fantasia (Emitido por)

    try:
        owner = Owner(
            email=body.email,
            username=body.username,
            valor=0.0,
            sub_base=sub_base_final,
            contato=owner_contato,
            nome_fantasia=nome_fantasia_db,
            ativo=True,
            ignorar_coleta=False,
        )
        db.add(owner)
        db.flush()

        user = User(
            email=body.email,
            username=body.username,
            password_hash=get_password_hash(body.password),
            contato=present(body.contato),
            nome=normalize_person_name(body.nome),
            sobrenome=normalize_person_name(body.sobrenome),
            status=True,
            role=1,
            coletador=False,
            sub_base=sub_base_final,
            must_change_password=False,
        )
        db.add(user)
        db.flush()

        if has_company_block:
            seller = BaseSellerDados(
                owner_id=owner.id_owner,
                base_id=None,
                cnpj=cnpj_digits,
                rua=present(body.rua),
                numero=present(body.numero),
                complemento=present(body.complemento) or None,
                bairro=present(body.bairro),
                cidade=present(body.cidade),
                estado=present(body.estado).upper()[:2] or None,
                cep=digits_only(body.cep),
            )
            db.add(seller)

        db.commit()
        db.refresh(owner)
        db.refresh(user)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return {
        "ok": True,
        "message": "Conta criada com sucesso.",
        "owner_id": owner.id_owner,
        "user_id": user.id,
        "sub_base": sub_base_final,
    }
