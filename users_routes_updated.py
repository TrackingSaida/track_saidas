from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional, List, Any
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db import get_db
from auth import get_current_user, get_password_hash, verify_password, DEFAULT_PASSWORD
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
    cnpj: Optional[str] = None
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
    cnpj: Optional[str] = None
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

    must_change_password: Optional[bool] = None

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
    cnpj: Optional[str] = None
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
    """Troca voluntária exige current_password; troca obrigatória (must_change_password) pode omitir."""
    current_password: Optional[str] = None
    new_password: str = Field(min_length=8)
    model_config = ConfigDict(from_attributes=True)


# ============================================================
# Helpers
# ============================================================


def _db_user_from_token(db: Session, token_user: User) -> User:
    """
    get_current_user() devolve User montado só pelo JWT (_user_from_claims), fora da sessão.
    Atribuir password_hash/nome/etc. a esse objeto não persiste no banco; é preciso carregar a linha.
    """
    uid = getattr(token_user, "id", None)
    if uid is None:
        raise HTTPException(401, "Token inválido")
    row = db.get(User, uid)
    if not row:
        raise HTTPException(404, "Usuário não encontrado")
    return row


def _sanitize_sub_base(sub_base: str) -> str:
    """
    Normaliza a sub_base para uso em domínio/username:
    - remove espaços
    - remove qualquer caractere que não seja letra ou número
    (ex.: 'Giro Express' -> 'GiroExpress', 'RUB_TEST1' -> 'RUBTEST1').
    """
    raw = (sub_base or "").strip().replace(" ", "")
    s = re.sub(r"[^A-Za-z0-9]", "", raw)
    return s if s else "migrado"


def _sub_base_domain(sub_base: Optional[str]) -> str:
    """Sub_base para domínio de email: sem espaços, minúsculo (ex.: Giro Express -> giroexpress)."""
    return _sanitize_sub_base(sub_base or "").lower() or "migrado"


def default_password_motoboy(sub_base: Optional[str]) -> str:
    """
    Senha padrão para motoboy quando não informada explicitamente.
    Mantemos a política de forçar troca de senha no primeiro acesso em outro lugar
    (ex.: obrigando o usuário a alterar a senha após login), então aqui usamos
    sempre '123456' como senha inicial fixa.
    """
    return DEFAULT_PASSWORD


def _first_word(s: Optional[str]) -> str:
    """Primeira palavra do texto, em minúsculo."""
    parts = (s or "").strip().split()
    return parts[0].lower() if parts else ""


def _last_word(s: Optional[str]) -> str:
    """Última palavra do texto, em minúsculo."""
    parts = (s or "").strip().split()
    return parts[-1].lower() if parts else ""


def _placeholder_username_from_nome(nome: Optional[str], sub_base: Optional[str] = None) -> str:
    """
    Placeholder de username: primeiro_nome.subbase (normalizada).
    Ex.: nome='Abacate Matheus', sub_base='Giro Express' -> 'abacate.giroexpress'.
    Quando não houver nome, usa apenas a sub_base normalizada.
    """
    first = _first_word(nome)
    base = _sanitize_sub_base(sub_base or "").lower()
    if first and base:
        return f"{first}.{base}"
    if first:
        return first
    return base


def _placeholder_email_from_nome_sobrenome(
    nome: Optional[str], sobrenome: Optional[str], sub_base: Optional[str]
) -> str:
    """Placeholder de email: primeiro_nome-ultimo_sobrenome@subbase.com (ex.: abacate-silva@giroexpress.com)."""
    first = _first_word(nome)
    last = _last_word(sobrenome)
    domain = _sub_base_domain(sub_base)
    if first and last:
        return f"{first}-{last}@{domain}.com"
    return ""


def _placeholder_email(user_id: int, sub_base: Optional[str]) -> str:
    """Fallback quando não há nome/sobrenome: sem-email{id}@{sub_base}.migrado.com"""
    return f"sem-email{user_id}@{_sanitize_sub_base(sub_base or '')}.migrado.com"


def _is_email_safe_for_display(raw: str) -> bool:
    """
    Retorna True se o valor é aceitável por EmailStr.
    Usa a própria validação do Pydantic para evitar 500 ao serializar.
    """
    if not raw:
        return False
    try:
        EmailStr.validate(raw)
        return True
    except Exception:
        return False


def _user_to_out(user: User) -> UserOut:
    """Serializa User para UserOut incluindo motoboy quando role=4.
    Usa fallbacks para campos obrigatórios quando o registro vem da migração
    ou tem dados incompletos, evitando 500 ao listar usuários."""
    try:
        user_id = int(getattr(user, "id", 0))
        sub_base = getattr(user, "sub_base", None)
        nome = getattr(user, "nome", None)
        sobrenome = getattr(user, "sobrenome", None)

        email_val = (user.email or "").strip()
        if not _is_email_safe_for_display(email_val):
            email_val = _placeholder_email_from_nome_sobrenome(nome, sobrenome, sub_base) or _placeholder_email(user_id, sub_base)

        username_val = (user.username or "").strip()
        if not username_val or username_val.startswith("sem_username"):
            username_val = _placeholder_username_from_nome(nome, sub_base) or username_val or "—"
        if not username_val:
            username_val = "—"

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
            "must_change_password": getattr(user, "must_change_password", None),
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
        sub_base = getattr(user, "sub_base", None)
        nome = getattr(user, "nome", None)
        sobrenome = getattr(user, "sobrenome", None)
        return UserOut(
            id=user_id,
            email=_placeholder_email_from_nome_sobrenome(nome, sobrenome, sub_base) or _placeholder_email(user_id, sub_base),
            username=_placeholder_username_from_nome(nome, sub_base) or "—",
            contato="—",
            status=getattr(user, "status", True),
            sub_base=getattr(user, "sub_base", None),
            nome=getattr(user, "nome", None),
            sobrenome=getattr(user, "sobrenome", None),
            role=getattr(user, "role", 2),
            coletador=getattr(user, "coletador", False),
            motoboy=None,
            must_change_password=getattr(user, "must_change_password", None),
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

    _ts = str(int(time.time() * 1000))

    # Para role != 4 (não-motoboy): username e e-mail obrigatórios; senha opcional (usa padrão quando vazia)
    if body.role != 4:
        u = (body.username or "").strip()
        e = (body.email or "").strip()
        p = (body.password or "").strip()
        if not u:
            raise HTTPException(422, "Username é obrigatório para este perfil.")
        if not e:
            raise HTTPException(422, "E-mail é obrigatório para este perfil.")
        if "@" not in e or "." not in e.split("@", 1)[-1]:
            raise HTTPException(422, "E-mail inválido.")
        if p and len(p) < 4:
            raise HTTPException(422, "Senha deve ter no mínimo 4 caracteres para este perfil.")
        username_val = u
        email_val = e
        if p:
            password_hash_val = get_password_hash(p)
        else:
            password_hash_val = get_password_hash(DEFAULT_PASSWORD)
    else:
        # Role 4 (motoboy): placeholders permitidos
        username_val = (body.username or "").strip() or _placeholder_username_from_nome(body.nome, sub_base) or _sanitize_sub_base(sub_base or "") or f"sem_username_{_ts}"
        email_val = (body.email or "").strip() or _placeholder_email_from_nome_sobrenome(body.nome, body.sobrenome, sub_base) or f"sem-email-{_ts}@{_sub_base_domain(sub_base)}.com"
        if (body.password or "").strip():
            password_hash_val = get_password_hash((body.password or "").strip())
        else:
            password_hash_val = get_password_hash(default_password_motoboy(sub_base))

    # Emails e usernames únicos
    email_raw = (body.email or "").strip()
    if email_raw:
        if db.scalar(select(User).where(User.email == email_raw)):
            raise HTTPException(409, "Email já existe.")

    # Username único POR sub_base (permite mesmo username em sub_bases diferentes)
    username_check = (username_val or "").strip()
    if username_check:
        exists_username = db.scalar(
            select(User).where(
                User.username == username_check,
                User.sub_base == sub_base,
            )
        )
        if exists_username:
            raise HTTPException(409, "Já existe um usuário com esse username nesta sub_base.")

    # Contato único (telefone/celular) — mesma sub_base
    contato_val = (body.contato or "").strip()
    if not contato_val:
        raise HTTPException(422, "Contato é obrigatório.")
    exists_contato = db.scalar(
        select(User).where(
            User.contato == contato_val,
            User.sub_base == sub_base,
        )
    )
    if exists_contato:
        raise HTTPException(409, "Contato já existe para esta sub_base.")

    # --- ROLE 4 (Motoboy): campos de endereço opcionais (motoboy provisório) ---

    # --- MAPEAR ROLE → COLETADOR (legado) ---
    coletador = (body.role == 3)

    try:
        new_user = User(
            email=email_val,
            password_hash=password_hash_val,
            username=username_val,
            contato=contato_val,
            nome=body.nome,
            sobrenome=body.sobrenome,
            status=True,
            role=body.role,
            coletador=coletador,
            sub_base=sub_base,
            must_change_password=True,
        )

        db.add(new_user)
        db.flush()

        # Se email era placeholder, gravar formato definitivo (nome-sobrenome@subbase.com ou fallback)
        if not (body.email or "").strip():
            new_user.email = _placeholder_email_from_nome_sobrenome(body.nome, body.sobrenome, sub_base) or _placeholder_email(new_user.id, sub_base)

        if body.role == 4:
            pode_ler_coleta = body.pode_ler_coleta if body.pode_ler_coleta is not None else False
            pode_ler_saida = body.pode_ler_saida if body.pode_ler_saida is not None else True
            if owner.ignorar_coleta:
                pode_ler_coleta = False

            motoboy = Motoboy(
                user_id=new_user.id,
                sub_base=sub_base,
                documento=(body.documento or "").strip(),
                cnpj=(body.cnpj or "").strip(),
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
            sub_base = getattr(u, "sub_base", None)
            nome = getattr(u, "nome", None)
            sobrenome = getattr(u, "sobrenome", None)
            out.append(UserOut(
                id=uid,
                email=_placeholder_email_from_nome_sobrenome(nome, sobrenome, sub_base) or _placeholder_email(uid, sub_base),
                username=_placeholder_username_from_nome(nome) or "—",
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

    # Validação de username único por sub_base ao editar
    if "username" in updates and updates["username"]:
        new_username = (updates["username"] or "").strip()
        if new_username and new_username != (user.username or ""):
            exists_username = db.scalar(
                select(User).where(
                    User.username == new_username,
                    User.sub_base == user.sub_base,
                    User.id != user.id,
                )
            )
            if exists_username:
                raise HTTPException(409, "Já existe um usuário com esse username nesta sub_base.")

    for field, value in updates.items():
        if field in user_fields:
            setattr(user, field, value)

    # Campos Motoboy (role=4)
    motoboy_fields = {
        "documento", "cnpj", "rua", "numero", "complemento", "bairro", "cidade", "estado", "cep",
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
                cnpj=(updates.get("cnpj") or "").strip(),
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
# POST /users/{id}/reset-password — Resetar para senha padrão
# ============================================================

@router.post("/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if getattr(current_user, "role", None) not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    user.password_hash = get_password_hash(DEFAULT_PASSWORD)
    user.must_change_password = True
    db.commit()

    return {"ok": True, "message": "Senha redefinida para a senha padrão."}


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
    db_user = _db_user_from_token(db, current_user)

    if payload.nome is not None:
        db_user.nome = payload.nome.strip() or None

    if payload.sobrenome is not None:
        db_user.sobrenome = payload.sobrenome.strip() or None

    if payload.contato is not None:
        contato = payload.contato.strip()
        if not contato:
            raise HTTPException(400, "Contato não pode ser vazio.")

        exists = db.query(User).filter(User.contato == contato, User.id != db_user.id).first()
        if exists:
            raise HTTPException(409, "Contato já em uso.")
        db_user.contato = contato

    if payload.email is not None:
        email = payload.email.strip()
        if not email:
            raise HTTPException(400, "Email não pode ser vazio.")

        exists = db.query(User).filter(User.email == email, User.id != db_user.id).first()
        if exists:
            raise HTTPException(409, "Email já em uso.")
        db_user.email = email

    db.commit()
    db.refresh(db_user)
    out = _user_to_out(db_user)
    full = UserFull.model_validate(out)
    if db_user.sub_base:
        owner = db.scalar(select(Owner).where(Owner.sub_base == db_user.sub_base))
        if owner:
            full.ignorar_coleta = bool(owner.ignorar_coleta)
    return full


# ============================================================
# POST /users/me/password
# ============================================================

@router.post("/me/password")
def change_password(
    payload: PasswordChangePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db_user = _db_user_from_token(db, current_user)

    must_change = bool(getattr(db_user, "must_change_password", False))
    if not must_change:
        cur = (payload.current_password or "").strip()
        if not cur:
            raise HTTPException(400, "Informe a senha atual.")
        if not verify_password(cur, db_user.password_hash):
            raise HTTPException(401, "Senha atual incorreta.")

    db_user.password_hash = get_password_hash(payload.new_password)
    db_user.must_change_password = False
    db.commit()
    return {"ok": True, "message": "Senha alterada com sucesso"}
