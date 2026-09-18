from __future__ import annotations

import logging
from datetime import date
from typing import Optional, List, Any
import re
import unicodedata

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from db import get_db
from name_normalizer import normalize_person_name
from auth import get_current_user, get_password_hash, verify_password, DEFAULT_PASSWORD, revoke_motoboy_refresh_tokens_for_user, bump_motoboy_claims_version
from entregador_legado_sync import (
    limpar_legado_entregador_ao_excluir_usuario,
    sincronizar_legado_entregador_com_status_usuario,
)
from models import User, Owner, Motoboy, MotoboySubBase
from base import _resolve_user_sub_base

router = APIRouter(prefix="/users", tags=["Users"])
logger = logging.getLogger("routes.users")


def _caller_is_root(current_user: User) -> bool:
    return getattr(current_user, "role", None) == 0


def _deny_non_root_managing_root(current_user: User, target_role: Optional[int]) -> None:
    """Admin (role=1) não pode gerenciar usuários root (role=0)."""
    if target_role == 0 and not _caller_is_root(current_user):
        raise HTTPException(403, "Não é permitido gerenciar usuário root.")


def _deny_non_root_assigning_root(current_user: User, new_role: Optional[int]) -> None:
    if new_role == 0 and not _caller_is_root(current_user):
        raise HTTPException(403, "Não é permitido criar ou promover usuário root.")


# ============================================================
# Schemas
# ============================================================

class MotoboyOut(BaseModel):
    id_motoboy: Optional[int] = None
    documento: Optional[str] = None
    cnpj: Optional[str] = None
    chave_pix: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    pode_ler_coleta: bool = False
    pode_realizar_coleta: bool = False
    pode_ler_saida: bool = True
    pode_digitar_codigo_manual: bool = False
    pode_lancar_avulso: bool = True
    avulso_exige_foto: bool = True

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    # Opcionais durante migração; placeholders são usados quando vazios (EmailStr não aceita "")
    email: Optional[str] = None
    password: Optional[str] = None
    username: Optional[str] = None
    contato: str

    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    data_nascimento: Optional[date] = None

    # admin=1, operador=2, coletador=3 (legado), motoboy=4
    role: int = Field(default=2)

    # Campos obrigatórios quando role=4
    documento: Optional[str] = None
    cnpj: Optional[str] = None
    chave_pix: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    pode_ler_coleta: Optional[bool] = None
    pode_realizar_coleta: Optional[bool] = None
    pode_ler_saida: Optional[bool] = None
    pode_digitar_codigo_manual: Optional[bool] = None
    pode_lancar_avulso: Optional[bool] = None
    avulso_exige_foto: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class UserOut(BaseModel):
    id: int
    email: Optional[EmailStr] = None
    username: str
    contato: str

    status: Optional[bool] = None
    sub_base: Optional[str] = None
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    data_nascimento: Optional[date] = None
    role: Optional[int] = None
    coletador: Optional[bool] = None
    motoboy: Optional[MotoboyOut] = None

    must_change_password: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class UserFull(UserOut):
    ignorar_coleta: Optional[bool] = None  # para desabilitar checkbox no frontend
    bloquear_saida_sem_coleta: Optional[bool] = None


class AdminUserUpdate(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[str] = None
    data_nascimento: Optional[date] = None
    status: Optional[bool] = None
    role: Optional[int] = None  # 1, 2, 3 ou 4

    # Campos motoboy (quando role=4)
    documento: Optional[str] = None
    cnpj: Optional[str] = None
    chave_pix: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    pode_ler_coleta: Optional[bool] = None
    pode_realizar_coleta: Optional[bool] = None
    pode_ler_saida: Optional[bool] = None
    pode_digitar_codigo_manual: Optional[bool] = None
    pode_lancar_avulso: Optional[bool] = None
    avulso_exige_foto: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class MotoboyPermissoesLoteIn(BaseModel):
    """Aplica permissão a todos os motoboys da sub_base do admin."""
    pode_lancar_avulso: Optional[bool] = None
    pode_digitar_codigo_manual: Optional[bool] = None
    avulso_exige_foto: Optional[bool] = None


class MotoboyPermissoesLoteOut(BaseModel):
    atualizados: int
    pode_lancar_avulso: Optional[bool] = None
    pode_digitar_codigo_manual: Optional[bool] = None
    avulso_exige_foto: Optional[bool] = None


class UserUpdatePayload(BaseModel):
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    contato: Optional[str] = None
    email: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PasswordChangePayload(BaseModel):
    """Troca voluntária exige current_password; troca obrigatória (must_change_password) pode omitir."""
    current_password: Optional[str] = None
    new_password: str = Field(min_length=8)
    model_config = ConfigDict(from_attributes=True)


# ============================================================
# Helpers
# ============================================================


def _normalize_optional_email(raw: Optional[str]) -> Optional[str]:
    """"" / whitespace → NULL. Valida formato só quando informado."""
    email = (raw or "").strip() or None
    if not email:
        return None
    if "@" not in email or "." not in email.split("@", 1)[-1]:
        raise HTTPException(422, "E-mail inválido.")
    return email


def _assert_email_unique(
    db: Session,
    email: Optional[str],
    exclude_user_id: Optional[int] = None,
) -> None:
    if not email:
        return
    stmt = select(User.id).where(User.email == email)
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    if db.scalar(stmt.limit(1)) is not None:
        raise HTTPException(409, "Email já existe.")


def _validate_data_nascimento(value: Optional[date]) -> Optional[date]:
    """Normaliza e valida data de nascimento (opcional). Rejeita data futura."""
    if value is None:
        return None
    if value > date.today():
        raise HTTPException(422, "Data de nascimento não pode ser futura.")
    return value


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


def _username_token(raw: Optional[str]) -> str:
    """
    Normaliza token para username:
    - remove acentos
    - mantém apenas [a-z0-9]
    """
    txt = (raw or "").strip().lower()
    if not txt:
        return ""
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(ch for ch in txt if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", txt)


def _normalize_username_for_compare(username: Optional[str]) -> str:
    return (username or "").strip().lower()


def _username_exists_in_sub_base(
    db: Session,
    sub_base: str,
    username: str,
    exclude_user_id: Optional[int] = None,
) -> bool:
    normalized = _normalize_username_for_compare(username)
    if not normalized:
        return False
    stmt = select(User.id).where(
        User.sub_base == sub_base,
        func.lower(func.trim(User.username)) == normalized,
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return db.scalar(stmt.limit(1)) is not None


def _generate_unique_username_for_sub_base(
    db: Session,
    sub_base: str,
    nome: Optional[str],
    sobrenome: Optional[str],
) -> str:
    """
    Gera username único na sub_base.
    Prioridade:
    1) primeiroNome.subbase
    2) primeiroNome.ultimoSobrenome
    3) primeiroNome.ultimoSobrenome.subbase
    4) sufixo incremental (ex.: ...2, ...3)
    """
    first = _username_token(_first_word(nome))
    last = _username_token(_last_word(sobrenome))
    base = _username_token(_sanitize_sub_base(sub_base or ""))

    candidates: list[str] = []
    if first and base:
        candidates.append(f"{first}.{base}")
    if first and last:
        candidates.append(f"{first}.{last}")
    if first and last and base:
        candidates.append(f"{first}.{last}.{base}")
    if first:
        candidates.append(first)
    if base:
        candidates.append(base)

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for cand in candidates:
        c = (cand or "").strip(".")
        if not c or c in seen:
            continue
        seen.add(c)
        unique_candidates.append(c)

    for cand in unique_candidates:
        if not _username_exists_in_sub_base(db, sub_base, cand):
            return cand

    root = unique_candidates[0] if unique_candidates else "user"
    i = 2
    while True:
        cand = f"{root}{i}"
        if not _username_exists_in_sub_base(db, sub_base, cand):
            return cand
        i += 1


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

        email_val = (user.email or "").strip() or None
        if email_val and not _is_email_safe_for_display(email_val):
            email_val = None

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
            "nome": normalize_person_name(user.nome),
            "sobrenome": normalize_person_name(user.sobrenome),
            "data_nascimento": getattr(user, "data_nascimento", None),
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
            email=None,
            username=_placeholder_username_from_nome(nome, sub_base) or "—",
            contato="—",
            status=getattr(user, "status", True),
            sub_base=getattr(user, "sub_base", None),
            nome=normalize_person_name(getattr(user, "nome", None)),
            sobrenome=normalize_person_name(getattr(user, "sobrenome", None)),
            data_nascimento=getattr(user, "data_nascimento", None),
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

    _deny_non_root_assigning_root(current_user, body.role)

    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(400, "Usuário atual não possui sub_base.")

    # Owner válido
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(400, f"Não existe Owner para a sub_base '{sub_base}'.")
    if not owner.ativo:
        raise HTTPException(403, "Owner desta sub_base está inativo.")

    # Staff (role != 4): username obrigatório; e-mail opcional. Motoboy: e-mail vazio → NULL.
    if body.role != 4:
        u = (body.username or "").strip()
        e = _normalize_optional_email(body.email)
        p = (body.password or "").strip()
        if not u:
            raise HTTPException(422, "Username é obrigatório para este perfil.")
        if p and len(p) < 4:
            raise HTTPException(422, "Senha deve ter no mínimo 4 caracteres para este perfil.")
        username_val = u
        email_val = e
        if p:
            password_hash_val = get_password_hash(p)
        else:
            password_hash_val = get_password_hash(DEFAULT_PASSWORD)
    else:
        username_manual = (body.username or "").strip()
        if username_manual:
            username_val = username_manual
        else:
            username_val = _generate_unique_username_for_sub_base(db, sub_base, body.nome, body.sobrenome)
        email_val = _normalize_optional_email(body.email)
        if (body.password or "").strip():
            password_hash_val = get_password_hash((body.password or "").strip())
        else:
            password_hash_val = get_password_hash(default_password_motoboy(sub_base))

    username_val = (username_val or "").strip()

    _assert_email_unique(db, email_val)

    # Username único POR sub_base (permite mesmo username em sub_bases diferentes)
    username_check = (username_val or "").strip()
    if username_check:
        if _username_exists_in_sub_base(db, sub_base, username_check):
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
    data_nascimento_val = _validate_data_nascimento(body.data_nascimento)

    try:
        new_user = User(
            email=email_val,
            password_hash=password_hash_val,
            username=username_val,
            contato=contato_val,
            nome=normalize_person_name(body.nome),
            sobrenome=normalize_person_name(body.sobrenome),
            data_nascimento=data_nascimento_val,
            status=True,
            role=body.role,
            coletador=coletador,
            sub_base=sub_base,
            must_change_password=True,
        )

        db.add(new_user)
        db.flush()

        if body.role == 4:
            # Defaults oficiais do Owner (Políticas gerais); body sobrescreve se enviado
            def_coleta = bool(getattr(owner, "default_pode_realizar_coleta", False))
            def_saida = bool(getattr(owner, "default_pode_ler_saida", True))
            def_digitar = bool(getattr(owner, "default_pode_digitar_codigo_manual", False))
            def_avulso = bool(getattr(owner, "default_pode_lancar_avulso", True))
            def_foto = bool(getattr(owner, "default_avulso_exige_foto", True))

            pode_ler_coleta = body.pode_ler_coleta if body.pode_ler_coleta is not None else def_coleta
            pode_realizar_coleta = (
                body.pode_realizar_coleta
                if body.pode_realizar_coleta is not None
                else (pode_ler_coleta if body.pode_ler_coleta is not None else def_coleta)
            )
            pode_ler_saida = body.pode_ler_saida if body.pode_ler_saida is not None else def_saida
            pode_digitar_codigo_manual = (
                body.pode_digitar_codigo_manual if body.pode_digitar_codigo_manual is not None else def_digitar
            )
            pode_lancar_avulso = (
                body.pode_lancar_avulso if body.pode_lancar_avulso is not None else def_avulso
            )
            avulso_exige_foto = (
                bool(body.avulso_exige_foto) if body.avulso_exige_foto is not None else def_foto
            )
            if not pode_lancar_avulso:
                avulso_exige_foto = False
            if owner.ignorar_coleta:
                pode_ler_coleta = False
                pode_realizar_coleta = False

            motoboy = Motoboy(
                user_id=new_user.id,
                sub_base=sub_base,
                documento=(body.documento or "").strip(),
                cnpj=(body.cnpj or "").strip(),
                chave_pix=(body.chave_pix or "").strip() or None,
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
                pode_realizar_coleta=pode_realizar_coleta,
                pode_ler_saida=pode_ler_saida,
                pode_digitar_codigo_manual=pode_digitar_codigo_manual,
                pode_lancar_avulso=pode_lancar_avulso,
                avulso_exige_foto=avulso_exige_foto,
                claims_version=0,
            )
            db.add(motoboy)
            db.flush()

            sb = MotoboySubBase(motoboy_id=motoboy.id_motoboy, sub_base=sub_base, ativo=True)
            db.add(sb)

        db.commit()
        db.refresh(new_user)

        return {"ok": True, "id": new_user.id}

    except IntegrityError as e:
        db.rollback()
        msg = str(getattr(e, "orig", e)).lower()
        if "username" in msg and ("already exists" in msg or "duplicate key" in msg):
            raise HTTPException(409, "Já existe um usuário com esse username nesta sub_base.")
        if "email" in msg and ("already exists" in msg or "duplicate key" in msg):
            raise HTTPException(409, "Email já existe.")
        if "contato" in msg and ("already exists" in msg or "duplicate key" in msg):
            raise HTTPException(409, "Contato já existe para esta sub_base.")
        logger.exception("Erro de integridade ao criar usuário: %s", e)
        raise HTTPException(409, "Conflito de dados ao criar usuário.")
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
            full.bloquear_saida_sem_coleta = bool(getattr(owner, "bloquear_saida_sem_coleta", False))
    return full


# ============================================================
# LISTAR MOTOBOYS (role=4) — para combo de atribuição no painel
# ============================================================

class MotoboyItem(BaseModel):
    id_motoboy: int
    nome: str
    pode_lancar_avulso: bool = True
    avulso_exige_foto: bool = False


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
        if u.motoboy and u.motoboy.id_motoboy and bool(getattr(u.motoboy, "ativo", True)):
            from motoboy_nome_utils import format_motoboy_nome_parts
            nome = format_motoboy_nome_parts(
                u.nome, u.sobrenome, u.username, motoboy_id=u.motoboy.id_motoboy
            )
            out.append(
                MotoboyItem(
                    id_motoboy=u.motoboy.id_motoboy,
                    nome=nome or f"Motoboy {u.motoboy.id_motoboy}",
                    pode_lancar_avulso=bool(getattr(u.motoboy, "pode_lancar_avulso", True)),
                    avulso_exige_foto=bool(getattr(u.motoboy, "avulso_exige_foto", False)),
                )
            )
    out.sort(key=lambda x: (x.nome or "").casefold())
    return out


@router.post("/motoboys/permissoes-lote", response_model=MotoboyPermissoesLoteOut)
def motoboys_permissoes_lote(
    body: MotoboyPermissoesLoteIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Aplica permissões a todos os motoboys da sub_base do admin."""
    if getattr(current_user, "role", None) not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    if (
        body.pode_lancar_avulso is None
        and body.pode_digitar_codigo_manual is None
        and body.avulso_exige_foto is None
    ):
        raise HTTPException(422, "Informe ao menos uma permissão para atualizar.")

    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(403, "Sub_base não definida.")

    motoboys = list(
        db.scalars(
            select(Motoboy).where(Motoboy.sub_base == sub_base)
        ).all()
    )
    atualizados = 0
    for m in motoboys:
        changed = False
        if body.pode_lancar_avulso is not None:
            m.pode_lancar_avulso = bool(body.pode_lancar_avulso)
            if not m.pode_lancar_avulso:
                m.avulso_exige_foto = False
            changed = True
        if body.pode_digitar_codigo_manual is not None:
            m.pode_digitar_codigo_manual = bool(body.pode_digitar_codigo_manual)
            changed = True
        if body.avulso_exige_foto is not None:
            m.avulso_exige_foto = bool(body.avulso_exige_foto) and bool(m.pode_lancar_avulso)
            changed = True
        if changed:
            m.claims_version = int(getattr(m, "claims_version", 0) or 0) + 1
            atualizados += 1

    db.commit()
    return MotoboyPermissoesLoteOut(
        atualizados=atualizados,
        pode_lancar_avulso=body.pode_lancar_avulso,
        pode_digitar_codigo_manual=body.pode_digitar_codigo_manual,
        avulso_exige_foto=body.avulso_exige_foto,
    )


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
    q = select(User).options(joinedload(User.motoboy)).where(User.sub_base == sub_base)
    # Admin não vê usuários root na listagem
    if getattr(current_user, "role", None) == 1:
        q = q.where(User.role != 0)
    users = db.scalars(q).all()
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
                email=None,
                username=_placeholder_username_from_nome(nome) or "—",
                contato="—",
                status=getattr(u, "status", True),
                sub_base=getattr(u, "sub_base", None),
                nome=getattr(u, "nome", None),
                sobrenome=getattr(u, "sobrenome", None),
                data_nascimento=getattr(u, "data_nascimento", None),
                role=getattr(u, "role", 2),
                coletador=getattr(u, "coletador", False),
                motoboy=None,
            ))
    return out


# ============================================================
# ANIVERSARIANTES — mesma sub_base, ativos com data_nascimento
# ============================================================

class AniversarianteItem(BaseModel):
    id: int
    nome: Optional[str] = None
    sobrenome: Optional[str] = None
    username: Optional[str] = None
    data_nascimento: date
    dia: int
    mes: int


class AniversariantesOut(BaseModel):
    ano_referencia: int
    mes: Optional[int] = None
    meses: dict[str, list[AniversarianteItem]]


@router.get("/aniversariantes", response_model=AniversariantesOut)
def list_aniversariantes(
    mes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista aniversariantes ativos da sub_base. mes=1..12 filtra; omitido/all = ano todo."""
    if getattr(current_user, "role", None) not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    sub_base = _resolve_user_sub_base(db, current_user)
    if not sub_base or not str(sub_base).strip():
        raise HTTPException(403, "Usuário sem sub_base definida. Faça login novamente.")

    mes_filtro: Optional[int] = None
    if mes is not None and str(mes).strip() and str(mes).strip().lower() != "all":
        try:
            mes_filtro = int(str(mes).strip())
        except ValueError:
            raise HTTPException(422, "Mês inválido. Use 1 a 12 ou all.")
        if mes_filtro < 1 or mes_filtro > 12:
            raise HTTPException(422, "Mês inválido. Use 1 a 12 ou all.")

    stmt = (
        select(User)
        .where(
            User.sub_base == sub_base,
            User.status.is_(True),
            User.data_nascimento.is_not(None),
        )
    )
    if mes_filtro is not None:
        stmt = stmt.where(func.extract("month", User.data_nascimento) == mes_filtro)

    users = db.scalars(stmt).all()

    meses: dict[str, list[AniversarianteItem]] = {str(i): [] for i in range(1, 13)}
    for u in users:
        dn = getattr(u, "data_nascimento", None)
        if not dn:
            continue
        item = AniversarianteItem(
            id=int(u.id),
            nome=normalize_person_name(u.nome),
            sobrenome=normalize_person_name(u.sobrenome),
            username=(u.username or "").strip() or None,
            data_nascimento=dn,
            dia=int(dn.day),
            mes=int(dn.month),
        )
        meses[str(item.mes)].append(item)

    for key in meses:
        meses[key].sort(key=lambda x: (x.dia, (x.nome or "").casefold(), (x.sobrenome or "").casefold()))

    return AniversariantesOut(
        ano_referencia=date.today().year,
        mes=mes_filtro,
        meses=meses,
    )


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

    _deny_non_root_managing_root(current_user, getattr(user, "role", None))

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

    _deny_non_root_managing_root(current_user, getattr(user, "role", None))

    owner = db.scalar(select(Owner).where(Owner.sub_base == current_user.sub_base))
    updates = payload.model_dump(exclude_unset=True)

    previous_role = getattr(user, "role", None)

    # ROLE → define COLETADOR (legado)
    if "role" in updates:
        _deny_non_root_assigning_root(current_user, updates["role"])
        user.role = updates["role"]
        user.coletador = (updates["role"] == 3)
        # Troca de perfil: invalida refresh de motoboy (JWT staff antigo cai no get_current_user).
        if updates["role"] != previous_role:
            try:
                revoke_motoboy_refresh_tokens_for_user(db, int(user.id), commit=False)
            except Exception:
                logger.exception(
                    "Falha ao revogar refresh tokens ao mudar role user_id=%s", user.id
                )

    # Campos User
    user_fields = {"nome", "sobrenome", "username", "contato", "email", "status", "role", "data_nascimento"}

    # Validação/normalização de username por perfil
    if "username" in updates:
        raw_username = updates.get("username")
        new_username = (raw_username or "").strip()
        current_username = (user.username or "").strip()

        if not new_username:
            if user.role == 4:
                nome_ref = updates.get("nome", user.nome)
                sobrenome_ref = updates.get("sobrenome", user.sobrenome)
                new_username = _generate_unique_username_for_sub_base(
                    db, user.sub_base or current_user.sub_base or "", nome_ref, sobrenome_ref
                )
            else:
                raise HTTPException(422, "Username é obrigatório para este perfil.")

        if (
            _normalize_username_for_compare(new_username) != _normalize_username_for_compare(current_username)
            and _username_exists_in_sub_base(db, user.sub_base or "", new_username, exclude_user_id=user.id)
        ):
            raise HTTPException(409, "Já existe um usuário com esse username nesta sub_base.")

        updates["username"] = new_username

    if "nome" in updates:
        updates["nome"] = normalize_person_name(updates.get("nome"))
    if "sobrenome" in updates:
        updates["sobrenome"] = normalize_person_name(updates.get("sobrenome"))
    if "data_nascimento" in updates:
        updates["data_nascimento"] = _validate_data_nascimento(updates.get("data_nascimento"))

    if "email" in updates:
        email_val = _normalize_optional_email(updates.get("email"))
        _assert_email_unique(db, email_val, exclude_user_id=user.id)
        updates["email"] = email_val

    for field, value in updates.items():
        if field in user_fields:
            setattr(user, field, value)

    # Sincroniza Motoboy/Entregador legado quando status muda
    if "status" in updates:
        sincronizar_legado_entregador_com_status_usuario(
            db,
            user,
            ativo=bool(user.status),
            remover_excecao_preco=not bool(user.status),
        )
        if not bool(user.status):
            try:
                revoke_motoboy_refresh_tokens_for_user(db, int(user.id), commit=False)
            except Exception:
                logger.exception(
                    "Falha ao revogar refresh tokens ao inativar user_id=%s", user.id
                )

    # Campos Motoboy (role=4)
    motoboy_fields = {
        "documento", "cnpj", "chave_pix", "rua", "numero", "complemento", "bairro", "cidade", "estado", "cep",
        "pode_ler_coleta", "pode_realizar_coleta", "pode_ler_saida", "pode_digitar_codigo_manual", "pode_lancar_avulso",
        "avulso_exige_foto",
    }
    sub_base = current_user.sub_base or ""
    if user.role == 4:
        if user.motoboy:
            for field in motoboy_fields:
                if field in updates:
                    val = updates[field]
                    if field == "chave_pix":
                        val = (val or "").strip() or None
                    if field == "pode_ler_coleta" and owner and owner.ignorar_coleta:
                        val = False
                    if field == "pode_realizar_coleta" and owner and owner.ignorar_coleta:
                        val = False
                    setattr(user.motoboy, field, val)
            if "pode_realizar_coleta" in updates:
                user.motoboy.pode_ler_coleta = bool(user.motoboy.pode_realizar_coleta)
            elif "pode_ler_coleta" in updates:
                user.motoboy.pode_realizar_coleta = bool(user.motoboy.pode_ler_coleta)
            if not bool(getattr(user.motoboy, "pode_lancar_avulso", True)):
                user.motoboy.avulso_exige_foto = False
            perm_keys = {
                "pode_ler_coleta", "pode_realizar_coleta", "pode_ler_saida",
                "pode_digitar_codigo_manual", "pode_lancar_avulso", "avulso_exige_foto",
            }
            if perm_keys & set(updates.keys()):
                bump_motoboy_claims_version(db, user.motoboy, commit=False)
        else:
            # Criar Motoboy ao mudar role para 4
            obrigatorios = ["documento", "rua", "numero", "bairro", "cidade", "cep"]
            faltando = [f for f in obrigatorios if not (updates.get(f) or "").strip()]
            if faltando:
                raise HTTPException(422, f"Campos obrigatórios para Motoboy: {', '.join(faltando)}")
            pode_ler_coleta = updates.get("pode_ler_coleta", False) or False
            pode_realizar_coleta = updates.get("pode_realizar_coleta", pode_ler_coleta) or False
            pode_ler_saida = updates.get("pode_ler_saida", True) if updates.get("pode_ler_saida") is not None else True
            pode_digitar_codigo_manual = (
                updates.get("pode_digitar_codigo_manual", True)
                if updates.get("pode_digitar_codigo_manual") is not None
                else True
            )
            pode_lancar_avulso = (
                updates.get("pode_lancar_avulso", True)
                if updates.get("pode_lancar_avulso") is not None
                else True
            )
            avulso_exige_foto = bool(updates.get("avulso_exige_foto", False)) and bool(pode_lancar_avulso)
            if owner and owner.ignorar_coleta:
                pode_ler_coleta = False
                pode_realizar_coleta = False
            motoboy = Motoboy(
                user_id=user.id,
                sub_base=sub_base,
                documento=(updates.get("documento") or "").strip(),
                cnpj=(updates.get("cnpj") or "").strip(),
                chave_pix=(updates.get("chave_pix") or "").strip() or None,
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
                pode_realizar_coleta=pode_realizar_coleta,
                pode_ler_saida=pode_ler_saida,
                pode_digitar_codigo_manual=bool(pode_digitar_codigo_manual),
                pode_lancar_avulso=bool(pode_lancar_avulso),
                avulso_exige_foto=avulso_exige_foto,
            )
            db.add(motoboy)
            db.flush()
            db.add(MotoboySubBase(motoboy_id=motoboy.id_motoboy, sub_base=sub_base, ativo=True))

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        msg = str(getattr(e, "orig", e)).lower()
        if "username" in msg and ("already exists" in msg or "duplicate key" in msg):
            raise HTTPException(409, "Já existe um usuário com esse username nesta sub_base.")
        if "email" in msg and ("already exists" in msg or "duplicate key" in msg):
            raise HTTPException(409, "Email já existe.")
        if "contato" in msg and ("already exists" in msg or "duplicate key" in msg):
            raise HTTPException(409, "Contato já existe para esta sub_base.")
        logger.exception("Erro de integridade ao atualizar usuário id=%s: %s", user_id, e)
        raise HTTPException(409, "Conflito de dados ao atualizar usuário.")

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

    _deny_non_root_managing_root(current_user, getattr(user, "role", None))

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

    user = db.scalars(
        select(User).options(joinedload(User.motoboy)).where(User.id == user_id)
    ).first()
    if not user:
        raise HTTPException(404, "Usuário não encontrado")

    if user.sub_base != current_user.sub_base:
        raise HTTPException(403, "Acesso negado.")

    _deny_non_root_managing_root(current_user, getattr(user, "role", None))

    # Limpa espelho legado (entregador + exceção de preço) antes do hard delete.
    try:
        limpar_legado_entregador_ao_excluir_usuario(db, user)
    except Exception:
        logger.exception(
            "Falha ao limpar entregador legado ao excluir user_id=%s", user_id
        )
        raise HTTPException(500, "Falha ao limpar dados vinculados do usuário.")

    try:
        revoke_motoboy_refresh_tokens_for_user(db, int(user.id), commit=False)
    except Exception:
        logger.exception(
            "Falha ao revogar refresh tokens ao excluir user_id=%s", user_id
        )

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
        db_user.nome = normalize_person_name(payload.nome)

    if payload.sobrenome is not None:
        db_user.sobrenome = normalize_person_name(payload.sobrenome)

    if payload.contato is not None:
        contato = payload.contato.strip()
        if not contato:
            raise HTTPException(400, "Contato não pode ser vazio.")

        exists = db.query(User).filter(User.contato == contato, User.id != db_user.id).first()
        if exists:
            raise HTTPException(409, "Contato já em uso.")
        db_user.contato = contato

    if "email" in payload.model_fields_set:
        email_val = _normalize_optional_email(payload.email)
        _assert_email_unique(db, email_val, exclude_user_id=db_user.id)
        db_user.email = email_val

    db.commit()
    db.refresh(db_user)
    out = _user_to_out(db_user)
    full = UserFull.model_validate(out)
    if db_user.sub_base:
        owner = db.scalar(select(Owner).where(Owner.sub_base == db_user.sub_base))
        if owner:
            full.ignorar_coleta = bool(owner.ignorar_coleta)
            full.bloquear_saida_sem_coleta = bool(getattr(owner, "bloquear_saida_sem_coleta", False))
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
    revoke_motoboy_refresh_tokens_for_user(db, int(db_user.id))
    db.commit()
    return {"ok": True, "message": "Senha alterada com sucesso"}
