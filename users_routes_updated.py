from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional, List, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db import get_db
from auth import get_current_user, get_password_hash, verify_password
from models import User, Owner, Motoboy, MotoboySubBase
from base import _resolve_user_sub_base

router = APIRouter(prefix="/users", tags=["Users"])
logger = logging.getLogger("routes.users")


# ============================================================
# Schemas
# ============================================================

class MotoboyOut(BaseModel):
    id_motoboy: Optional[int] = None
    documento: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    pode_ler_coleta: bool = False
    pode_ler_saida: bool = True

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    # Opcionais durante migração; placeholders são usados quando vazios (EmailStr não aceita "")
    email: Optional[str] = None
    password: Optional[str] = None
    username: Optional[str] = None
    contato: str

    nome: Optional[str] = None
    sobrenome: Optional[str] = None

    # admin=1, operador=2, coletador=3 (legado), motoboy=4
    role: int = Field(default=2)

    # Campos obrigatórios quando role=4
    documento: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    pode_ler_coleta: Optional[bool] = None
    pode_ler_saida: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    username: str
    contato: str

    status: Optional[bool] = None
    sub_base: Optional[str] = None
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    role: Optional[int] = None
    coletador: Optional[bool] = None
    motoboy: Optional[MotoboyOut] = None

    model_config = ConfigDict(from_attributes=True)


class UserFull(UserOut):
    ignorar_coleta: Optional[bool] = None  # para desabilitar checkbox no frontend


class AdminUserUpdate(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[EmailStr] = None
    status: Optional[bool] = None
    role: Optional[int] = None  # 1, 2, 3 ou 4

    # Campos motoboy (quando role=4)
    documento: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    pode_ler_coleta: Optional[bool] = None
    pode_ler_saida: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class UserUpdatePayload(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[EmailStr] = None

    model_config = ConfigDict(from_attributes=True)


class PasswordChangePayload(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)
    model_config = ConfigDict(from_attributes=True)


# ============================================================
# Helpers
# ============================================================

def _sanitize_sub_base(sub_base: str) -> str:
    """Remove espaços para usar em email (ex.: 'Giro Express' -> 'GiroExpress')."""
    s = (sub_base or "").strip().replace(" ", "")
    return s if s else "migrado"


def _placeholder_email(user_id: int, sub_base: Optional[str]) -> str:
    """Placeholder de email exibível: sem-email{id}@{sub_base}.migrado.local"""
    return f"sem-email{user_id}@{_sanitize_sub_base(sub_base or '')}.migrado.local"


def _is_email_safe_for_display(raw: str) -> bool:
    """Retorna True se o valor é aceitável por EmailStr (sem espaço, formato básico)."""
    if not raw or " " in raw or "@" not in raw:
        return False
    parts = raw.split("@", 1)
    return len(parts) == 2 and len(parts[0]) >= 1 and "." in parts[1] and len(parts[1]) >= 4


def _user_to_out(user: User) -> UserOut:
    """Serializa User para UserOut incluindo motoboy quando role=4.
    Usa fallbacks para campos obrigatórios quando o registro vem da migração
    ou tem dados incompletos, evitando 500 ao listar usuários."""
    try:
        user_id = int(getattr(user, "id", 0))
        email_val = (user.email or "").strip()
        if not _is_email_safe_for_display(email_val):
            email_val = _placeholder_email(user_id, getattr(user, "sub_base", None))
        username_val = (user.username or "").strip() or "—"
        contato_val = (user.contato or "").strip() or "—"

        data: dict[str, Any] = {
            "id": user_id,
            "email": email_val,
            "username": username_val,
            "contato": contato_val,
            "status": getattr(user, "status", True),
            "sub_base": user.sub_base,
            "nome": user.nome,
            "sobrenome": user.sobrenome,
            "role": getattr(user, "role", 2),
            "coletador": getattr(user, "coletador", False),
            "motoboy": None,
        }
        if getattr(user, "role", None) == 4 and hasattr(user, "motoboy") and user.motoboy:
            try:
                data["motoboy"] = MotoboyOut.model_validate(user.motoboy)
            except Exception:
                logger.warning("Motoboy id=%s serialization skipped for user id=%s", getattr(user.motoboy, "id_motoboy", None), user.id)
        return UserOut(**data)
    except Exception as e:
        logger.warning("_user_to_out fallback for user id=%s: %s", getattr(user, "id", None), e)
        user_id = int(getattr(user, "id", 0))
        return UserOut(
            id=user_id,
            email=_placeholder_email(user_id, getattr(user, "sub_base", None)),
            username="—",
            contato="—",
            status=getattr(user, "status", True),
            sub_base=getattr(user, "sub_base", None),
            nome=getattr(user, "nome", None),
            sobrenome=getattr(user, "sobrenome", None),
            role=getattr(user, "role", 2),
            coletador=getattr(user, "coletador", False),
            motoboy=None,
        )


# ============================================================
# POST /users — CRIAR USUÁRIO COM SUB_BASE AUTOMÁTICA
# ============================================================

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cria usuário herdando sub_base e setando coletador baseado no role. Role 4 = Motoboy."""

    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(400, "Usuário atual não possui sub_base.")

    # Owner válido
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(400, f"Não existe Owner para a sub_base '{sub_base}'.")
    if not owner.ativo:
        raise HTTPException(403, "Owner desta sub_base está inativo.")

    # Resolver email/username/senha (opcionais durante migração)
    _ts = str(int(time.time() * 1000))
    # Placeholder temporário até ter o id; após flush será sem-email{id}@{sub_base}.migrado.local
    email_val = (body.email or "").strip() or f"sem-email-{_ts}@{_sanitize_sub_base(sub_base)}.migrado.local"
    username_val = (body.username or "").strip() or f"sem_username_{_ts}"
    if (body.password or "").strip():
        password_hash_val = get_password_hash((body.password or "").strip())
    else:
        password_hash_val = get_password_hash("migrado_trocar_senha")

    # Emails e usernames únicos (só quando informados pelo usuário)
    if body.email and (body.email or "").strip():
        if db.scalar(select(User).where(User.email == (body.email or "").strip())):
            raise HTTPException(409, "Email já existe.")
    if body.username and (body.username or "").strip():
        if db.scalar(select(User).where(User.username == (body.username or "").strip())):
            raise HTTPException(409, "Username já existe.")

    # --- ROLE 4 (Motoboy): campos de endereço opcionais (motoboy provisório) ---

    # --- MAPEAR ROLE → COLETADOR (legado) ---
    coletador = (body.role == 3)

    try:
        new_user = User(
            email=email_val,
            password_hash=password_hash_val,
            username=username_val,
            contato=body.contato,
            nome=body.nome,
            sobrenome=body.sobrenome,
            status=True,
            role=body.role,
            coletador=coletador,
            sub_base=sub_base
        )

        db.add(new_user)
        db.flush()

        # Atualizar email placeholder para formato definitivo: sem-email{id}@{sub_base}.migrado.local
        if not (body.email or "").strip():
            new_user.email = _placeholder_email(new_user.id, sub_base)

        if body.role == 4:
            pode_ler_coleta = body.pode_ler_coleta if body.pode_ler_coleta is not None else False
            pode_ler_saida = body.pode_ler_saida if body.pode_ler_saida is not None else True
            if owner.ignorar_coleta:
                pode_ler_coleta = False

            motoboy = Motoboy(
                user_id=new_user.id,
                sub_base=sub_base,
                documento=(body.documento or "").strip(),
                rua=(body.rua or "").strip(),
                numero=(body.numero or "").strip(),
                complemento=(body.complemento or "").strip() or None,
                bairro=(body.bairro or "").strip(),
                cidade=(body.cidade or "").strip(),
                estado=(body.estado or "").strip() or None,
                cep=(body.cep or "").strip(),
                ativo=True,
                data_cadastro=date.today(),
                pode_ler_coleta=pode_ler_coleta,
                pode_ler_saida=pode_ler_saida,
            )
            db.add(motoboy)
            db.flush()

            sb = MotoboySubBase(motoboy_id=motoboy.id_motoboy, sub_base=sub_base, ativo=True)
            db.add(sb)

        db.commit()
        db.refresh(new_user)

        return {"ok": True, "id": new_user.id}

    except Exception as e:
        db.rollback()
        logger.exception("Erro ao criar usuário: %s", e)
        raise HTTPException(500, "Erro interno ao criar usuário.")


# ============================================================
# GET /users/me
# ============================================================

@router.get("/me", response_model=UserFull)
def read_current_user(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = db.scalars(
        select(User).options(joinedload(User.motoboy)).where(User.id == current_user.id)
    ).first()
    if not user:
        raise HTTPException(404, "Usuário não encontrado")
    out = _user_to_out(user)
    full = UserFull.model_validate(out)
    if user.sub_base:
        owner = db.scalar(select(Owner).where(Owner.sub_base == user.sub_base))
        if owner:
            full.ignorar_coleta = bool(owner.ignorar_coleta)
    return full


# ============================================================
# LISTAR MOTOBOYS (role=4) — para combo de atribuição no painel
# ============================================================

class MotoboyItem(BaseModel):
    id_motoboy: int
    nome: str


@router.get("/motoboys", response_model=list[MotoboyItem])
def list_motoboys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista motoboys (usuários role=4) da mesma sub_base. Uso: atribuição de saídas no painel."""
    if getattr(current_user, "role", 0) not in (0, 1, 2):
        raise HTTPException(403, "Acesso negado.")
    sub_base = _resolve_user_sub_base(db, current_user)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(403, "Sub_base não definida.")
    users = db.scalars(
        select(User).options(joinedload(User.motoboy)).where(
            User.sub_base == sub_base,
            User.role == 4,
            User.status.is_(True),
        )
    ).all()
    out = []
    for u in users:
        if u.motoboy and u.motoboy.id_motoboy:
            nome = f"{u.nome or ''} {u.sobrenome or ''}".strip() or u.username or ""
            out.append(MotoboyItem(id_motoboy=u.motoboy.id_motoboy, nome=nome or f"Motoboy {u.motoboy.id_motoboy}"))
    return out


# ============================================================
# LISTAR USERS — APENAS MESMA SUB_BASE
# ============================================================

@router.get("/all", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Lista usuários apenas da mesma sub_base do solicitante (sub_base obtida do banco)."""
    if getattr(current_user, "role", None) not in (0, 1):
        raise HTTPException(403, "Apenas admin podem listar usuários.")

    sub_base = _resolve_user_sub_base(db, current_user)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(403, "Usuário sem sub_base definida. Faça login novamente.")
    users = db.scalars(
        select(User).options(joinedload(User.motoboy)).where(User.sub_base == sub_base)
    ).all()
    out = []
    for u in users:
        try:
            out.append(_user_to_out(u))
        except Exception as e:
            logger.warning("list_users: fallback para user id=%s: %s", getattr(u, "id", None), e)
            uid = int(getattr(u, "id", 0))
            out.append(UserOut(
                id=uid,
                email=_placeholder_email(uid, getattr(u, "sub_base", None)),
                username="—",
                contato="—",
                status=getattr(u, "status", True),
                sub_base=getattr(u, "sub_base", None),
                nome=getattr(u, "nome", None),
                sobrenome=getattr(u, "sobrenome", None),
                role=getattr(u, "role", 2),
                coletador=getattr(u, "coletador", False),
                motoboy=None,
            ))
    return out


# ============================================================
# GET USER BY ID — respeita sub_base
# ============================================================

@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user = db.scalars(
        select(User).options(joinedload(User.motoboy)).where(User.id == user_id)
    ).first()
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    return _user_to_out(user)


# ============================================================
# PATCH /users/{id} — Atualização ADMIN
# ============================================================

@router.patch("/{user_id}", response_model=UserOut)
def admin_update_user(
    user_id: int,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    user = db.scalars(
        select(User).options(joinedload(User.motoboy)).where(User.id == user_id)
    ).first()
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    owner = db.scalar(select(Owner).where(Owner.sub_base == current_user.sub_base))
    updates = payload.model_dump(exclude_unset=True)

    # ROLE → define COLETADOR (legado)
    if "role" in updates:
        user.role = updates["role"]
        user.coletador = (updates["role"] == 3)

    # Campos User
    user_fields = {"nome", "sobrenome", "username", "contato", "email", "status", "role"}
    for field, value in updates.items():
        if field in user_fields:
            setattr(user, field, value)

    # Campos Motoboy (role=4)
    motoboy_fields = {
        "documento", "rua", "numero", "complemento", "bairro", "cidade", "estado", "cep",
        "pode_ler_coleta", "pode_ler_saida"
    }
    sub_base = current_user.sub_base or ""
    if user.role == 4:
        if user.motoboy:
            for field in motoboy_fields:
                if field in updates:
                    val = updates[field]
                    if field == "pode_ler_coleta" and owner and owner.ignorar_coleta:
                        val = False
                    setattr(user.motoboy, field, val)
        else:
            # Criar Motoboy ao mudar role para 4
            obrigatorios = ["documento", "rua", "numero", "bairro", "cidade", "cep"]
            faltando = [f for f in obrigatorios if not (updates.get(f) or "").strip()]
            if faltando:
                raise HTTPException(422, f"Campos obrigatórios para Motoboy: {', '.join(faltando)}")
            pode_ler_coleta = updates.get("pode_ler_coleta", False) or False
            pode_ler_saida = updates.get("pode_ler_saida", True) if updates.get("pode_ler_saida") is not None else True
            if owner and owner.ignorar_coleta:
                pode_ler_coleta = False
            motoboy = Motoboy(
                user_id=user.id,
                sub_base=sub_base,
                documento=(updates.get("documento") or "").strip(),
                rua=(updates.get("rua") or "").strip(),
                numero=(updates.get("numero") or "").strip(),
                complemento=(updates.get("complemento") or "").strip() or None,
                bairro=(updates.get("bairro") or "").strip(),
                cidade=(updates.get("cidade") or "").strip(),
                estado=(updates.get("estado") or "").strip() or None,
                cep=(updates.get("cep") or "").strip(),
                ativo=True,
                data_cadastro=date.today(),
                pode_ler_coleta=pode_ler_coleta,
                pode_ler_saida=pode_ler_saida,
            )
            db.add(motoboy)
            db.flush()
            db.add(MotoboySubBase(motoboy_id=motoboy.id_motoboy, sub_base=sub_base, ativo=True))

    db.commit()
    db.refresh(user)
    return _user_to_out(user)


# ============================================================
# DELETE USER
# ============================================================

@router.delete("/{user_id}", status_code=200)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    db.delete(user)
    db.commit()
    return {"ok": True, "deleted": user_id}


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
