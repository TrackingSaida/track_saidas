from __future__ import annotations

from typing import Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db import get_db
from auth import get_current_user, get_password_hash, verify_password
from models import User, Owner, Motoboy
from deps import allow

router = APIRouter(prefix="/users", tags=["Users"])
logger = logging.getLogger("routes.users")


# ============================================================
# Schemas
# ============================================================

ROLE_MOTOBOY = 4


class MotoboyCreate(BaseModel):
    """Dados do motoboy para criação/edição (role = 4)."""
    documento: str = Field(min_length=1, description="CPF ou RG")
    telefone: Optional[str] = None
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: Optional[str] = None
    bairro: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    estado: Optional[str] = None
    cep: str = Field(min_length=1)
    model_config = ConfigDict(from_attributes=True)


class MotoboyOut(BaseModel):
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    documento: Optional[str] = None
    telefone: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4)
    username: str = Field(min_length=3)
    contato: str
    sub_base: str = Field(min_length=1, description="sub_base deve existir e ter Owner")

    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    status: Optional[bool] = True
    role: Optional[int] = 2

    # frontend: admin=1, operador=2, coletador=3, motoboy=4
    # Para role = 4 (Motoboy): obrigatório
    motoboy: Optional[MotoboyCreate] = None

    model_config = ConfigDict(from_attributes=True)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    contato: str
    role: Optional[int] = None
    status: Optional[bool] = None
    sub_base: Optional[str] = None
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    motoboy: Optional[MotoboyOut] = None
    coletador: Optional[bool] = None
    model_config = ConfigDict(from_attributes=True)


class UserFull(UserOut):
    pass


class AdminUserUpdate(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[EmailStr] = None
    status: Optional[bool] = None
    role: Optional[int] = None  # 1, 2, 3 ou 4 (motoboy)
    model_config = ConfigDict(from_attributes=True)


class UserUpdatePayload(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[EmailStr] = None

    model_config = ConfigDict(from_attributes=True)


class UserUpdateAdminPayload(BaseModel):
    """Payload para edição de usuário por admin (PATCH /users/{id})."""
    nome: Optional[str] = Field(default=None)
    sobrenome: Optional[str] = Field(default=None)
    contato: Optional[str] = Field(default=None)
    email: Optional[EmailStr] = Field(default=None)
    status: Optional[bool] = Field(default=None)
    role: Optional[int] = Field(default=None)
    motoboy: Optional[MotoboyCreate] = Field(default=None)
    model_config = ConfigDict(from_attributes=True)


class PasswordChangePayload(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)
    model_config = ConfigDict(from_attributes=True)


# ============================================================
# POST /users — CRIAR USUÁRIO COM SUB_BASE AUTOMÁTICA
# ============================================================

def _validate_motoboy_endereco(m: MotoboyCreate) -> None:
    """Exige endereço completo para motoboy. Levanta HTTP 422 se incompleto."""
    if not m:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Para perfil Motoboy é obrigatório informar os dados do motoboy (documento e endereço completo).",
        )
    campos = [m.rua, m.numero, m.bairro, m.cidade, m.cep]
    if not all(campos):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Para perfil Motoboy são obrigatórios: documento, rua, número, bairro, cidade e CEP.",
        )


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    """Cria um novo usuário. Se role=4 (Motoboy), cria também registro em motoboys."""

    role = body.role if body.role is not None else 2
    if role == ROLE_MOTOBOY:
        _validate_motoboy_endereco(body.motoboy)

    try:
        payload_log = body.model_dump(exclude={"password"})
        payload_log["password"] = "***"
    except Exception:
        payload_log = "erro ao processar payload"
    logger.info("POST /users payload=%s", payload_log)

    exists_email = db.scalars(select(User).where(User.email == body.email)).first()
    if exists_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="endereço de email já existente"
        )

    exists_username = db.scalars(select(User).where(User.username == body.username)).first()
    if exists_username:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username já existe"
        )

    sub_base = body.sub_base
    if not sub_base:
        raise HTTPException(400, "sub_base é obrigatório.")
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(400, f"Não existe Owner cadastrado para a sub_base '{sub_base}'.")
    if owner.ativo is False:
        raise HTTPException(403, f"O Owner da sub_base '{sub_base}' está inativo.")

    coletador = (role == 3)

    try:
        new_user = User(
            email=body.email,
            password_hash=get_password_hash(body.password),
            username=body.username,
            contato=body.contato,
            nome=body.nome,
            sobrenome=body.sobrenome,
            status=True if body.status is None else body.status,
            sub_base=sub_base,
            role=role,
            coletador=coletador,
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        if role == ROLE_MOTOBOY and body.motoboy:
            m = body.motoboy
            motoboy = Motoboy(
                user_id=new_user.id,
                sub_base=sub_base,
                documento=m.documento.strip() or None,
                telefone=m.telefone.strip() if m.telefone else None,
                rua=m.rua.strip(),
                numero=m.numero.strip(),
                complemento=m.complemento.strip() if m.complemento else None,
                bairro=m.bairro.strip(),
                cidade=m.cidade.strip(),
                estado=m.estado.strip() if m.estado else None,
                cep=m.cep.strip(),
            )
            db.add(motoboy)
            db.commit()

        logger.info("User criado com sucesso id=%s", new_user.id)
        return {"ok": True, "action": "created", "id": new_user.id}

    except SQLAlchemyError as e:
        db.rollback()
        logger.exception("SQLAlchemyError ao criar user: %s", e)
        raise
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Erro ao criar usuário: %s", e)
        raise HTTPException(500, "Erro interno ao criar usuário.")


# ============================================================
# GET /users/me — usuário logado (role sempre na resposta para telas de config)
# ============================================================

@router.get("/me", response_model=UserFull)
def read_current_user(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Garantir que role sempre venha como int (frontend exige para Configurações = só admin/root)
    role = getattr(current_user, "role", None)
    if role is None:
        # Token antigo ou sem role: buscar no banco
        u = db.get(User, current_user.id)
        role = int(u.role) if u and u.role is not None else 2
    else:
        role = int(role)
    current_user.role = role
    return current_user


# =========================
# GET /users/all — lista todos (admin)
# =========================
@router.get("/all")
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(allow(0, 1)),
):
    """Lista todos os usuários. Para role=4 inclui dados do motoboy."""
    stmt = select(User).options(joinedload(User.motoboy)).order_by(User.id)
    users = db.scalars(stmt).unique().all()
    result = []
    for u in users:
        item = {
            "id": u.id,
            "email": u.email,
            "username": u.username,
            "contato": u.contato,
            "nome": u.nome,
            "sobrenome": u.sobrenome,
            "sub_base": u.sub_base,
            "status": u.status,
            "role": int(u.role) if u.role is not None else 2,
        }
        if u.motoboy:
            item["motoboy"] = MotoboyOut.model_validate(u.motoboy)
        else:
            item["motoboy"] = None
        result.append(item)
    return result


# =========================
# GET /users/{user_id}
# =========================
@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, db: Session = Depends(get_db)):
    stmt = select(User).where(User.id == user_id).options(joinedload(User.motoboy))
    user = db.scalars(stmt).unique().one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuário não encontrado"
        )
    out = {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "contato": user.contato,
        "nome": user.nome,
        "sobrenome": user.sobrenome,
        "sub_base": user.sub_base,
        "status": user.status,
        "role": int(user.role) if user.role is not None else 2,
        "motoboy": MotoboyOut.model_validate(user.motoboy) if user.motoboy else None,
    }
    return UserOut(**out)


# ============================================================
# PATCH /users/me
# ============================================================

@router.patch("/me", response_model=UserFull)
def update_current_user(
    payload: UserUpdatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.nome is not None:
        current_user.nome = payload.nome.strip() or None

    if payload.sobrenome is not None:
        current_user.sobrenome = payload.sobrenome.strip() or None

    if payload.contato is not None:
        contato = payload.contato.strip()
        if not contato:
            raise HTTPException(400, "Contato não pode ser vazio.")

        exists = db.query(User).filter(User.contato == contato, User.id != current_user.id).first()
        if exists:
            raise HTTPException(409, "Contato já em uso.")
        current_user.contato = contato

    if payload.email is not None:
        email = payload.email.strip()
        if not email:
            raise HTTPException(400, "Email não pode ser vazio.")

        exists = db.query(User).filter(User.email == email, User.id != current_user.id).first()
        if exists:
            raise HTTPException(409, "Email já em uso.")
        current_user.email = email

    db.commit()
    db.refresh(current_user)
    return current_user


# =========================
# PATCH /users/{user_id} — edição por admin
# =========================
@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdateAdminPayload,
    db: Session = Depends(get_db),
    _: User = Depends(allow(0, 1)),
):
    """Atualiza usuário (admin). Se role=4, atualiza também dados do motoboy."""
    stmt = select(User).where(User.id == user_id).options(joinedload(User.motoboy))
    user = db.scalars(stmt).unique().one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")

    if payload.nome is not None:
        user.nome = payload.nome.strip() or None
    if payload.sobrenome is not None:
        user.sobrenome = payload.sobrenome.strip() or None
    if payload.contato is not None:
        contato = payload.contato.strip()
        if contato:
            other = db.scalars(select(User).where(User.contato == contato, User.id != user_id)).first()
            if other:
                raise HTTPException(409, "Já existe um usuário com esse contato.")
            user.contato = contato
    if payload.email is not None:
        email = payload.email.strip()
        if email:
            other = db.scalars(select(User).where(User.email == email, User.id != user_id)).first()
            if other:
                raise HTTPException(409, "Já existe um usuário com esse e-mail.")
            user.email = email
    if payload.status is not None:
        user.status = payload.status
    if payload.role is not None:
        user.role = payload.role
        user.coletador = (payload.role == 3)

    # Motoboy: se usuário é role 4 e veio payload.motoboy, atualizar ou criar
    if (int(user.role) == ROLE_MOTOBOY or (payload.role is not None and payload.role == ROLE_MOTOBOY)) and payload.motoboy:
        _validate_motoboy_endereco(payload.motoboy)
        m = payload.motoboy
        if user.motoboy:
            user.motoboy.documento = m.documento.strip() or None
            user.motoboy.telefone = m.telefone.strip() if m.telefone else None
            user.motoboy.rua = m.rua.strip()
            user.motoboy.numero = m.numero.strip()
            user.motoboy.complemento = m.complemento.strip() if m.complemento else None
            user.motoboy.bairro = m.bairro.strip()
            user.motoboy.cidade = m.cidade.strip()
            user.motoboy.estado = m.estado.strip() if m.estado else None
            user.motoboy.cep = m.cep.strip()
        else:
            motoboy = Motoboy(
                user_id=user.id,
                sub_base=user.sub_base,
                documento=m.documento.strip() or None,
                telefone=m.telefone.strip() if m.telefone else None,
                rua=m.rua.strip(),
                numero=m.numero.strip(),
                complemento=m.complemento.strip() if m.complemento else None,
                bairro=m.bairro.strip(),
                cidade=m.cidade.strip(),
                estado=m.estado.strip() if m.estado else None,
                cep=m.cep.strip(),
            )
            db.add(motoboy)
    elif payload.role is not None and payload.role == ROLE_MOTOBOY and not payload.motoboy:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Para perfil Motoboy são obrigatórios os dados do motoboy (documento e endereço completo).",
        )

    db.commit()
    db.refresh(user)
    if user.motoboy:
        db.refresh(user.motoboy)
    stmt2 = select(User).where(User.id == user_id).options(joinedload(User.motoboy))
    user = db.scalars(stmt2).unique().one()
    out = {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "contato": user.contato,
        "nome": user.nome,
        "sobrenome": user.sobrenome,
        "sub_base": user.sub_base,
        "status": user.status,
        "role": int(user.role) if user.role is not None else 2,
        "motoboy": MotoboyOut.model_validate(user.motoboy) if user.motoboy else None,
    }
    return UserOut(**out)


# =========================
# DELETE /users/{user_id}
# =========================
@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(allow(0, 1)),
):
    """Remove usuário. ON DELETE CASCADE remove registro em motoboys se existir."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    db.delete(user)
    db.commit()
    return {"ok": True, "message": "Usuário excluído"}


# ============================================================
# POST /users/me/password
# ============================================================

@router.post("/me/password")
def change_password(
    payload: PasswordChangePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(401, "Senha atual incorreta.")

    current_user.password_hash = get_password_hash(payload.new_password)
    db.commit()
    return {"ok": True, "message": "Senha alterada com sucesso"}
