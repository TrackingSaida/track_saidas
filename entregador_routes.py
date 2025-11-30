from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, AliasChoices
from sqlalchemy import select, or_
from sqlalchemy.orm import Session
from datetime import date

from db import get_db
from auth import get_current_user, get_password_hash
from models import User, Entregador

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================================================
# SCHEMAS
# =========================================================
class EntregadorCreate(BaseModel):
    # --- dados do entregador (obrigatórios) ---
    nome: str = Field(min_length=1)
    telefone: str = Field(min_length=1)
    documento: str = Field(min_length=1)

    # --- endereço (obrigatórios) ---
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: str = Field(min_length=1)
    cep: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    bairro: str = Field(min_length=1)

    # --- perfil coletador (opcional) ---
    coletador: Optional[bool] = False
    username_entregador: Optional[str] = None
    senha: Optional[str] = Field(default=None, validation_alias=AliasChoices("senha"))

    model_config = ConfigDict(from_attributes=True)


class EntregadorUpdate(BaseModel):
    # atualização parcial: só altera o que vier
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None

    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None

    coletador: Optional[bool] = None
    username_entregador: Optional[str] = None
    senha: Optional[str] = None
    ativo: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class EntregadorOut(BaseModel):
    id_entregador: int
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    ativo: bool
    data_cadastro: Optional[date] = None

    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None

    coletador: bool
    username_entregador: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# =========================================================
# HELPERS
# =========================================================
def _resolve_user_base(db: Session, current_user) -> str:
    """Resolve a sub_base do usuário autenticado"""
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base

    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário em 'users'.")


def _get_owned_entregador(db: Session, sub_base_user: str, id_entregador: int) -> Entregador:
    """Busca o entregador e valida se pertence à mesma sub_base"""
    obj = db.get(Entregador, id_entregador)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj


def _find_matching_user(db: Session, sub_base: str, username_ent: Optional[str]) -> Optional[User]:
    """Localiza um user vinculado ao entregador"""
    if not username_ent:
        return None
    stmt = select(User).where(
        User.sub_base == sub_base,
        or_(
            User.username == username_ent,
            User.username_entregador == username_ent,
        ),
    )
    return db.scalars(stmt).first()

# =========================================================
# ROTAS
# =========================================================

def _optional(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_entregador(
    body: EntregadorCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Cria um entregador e, se 'coletador=True', também cria um novo usuário em 'users':
      - password_hash recebe o hash da senha
      - username = username_entregador
      - contato = telefone
      - nome = nome
      - coletador = True
      - role = 3
      - sub_base = do solicitante
      - status = True
    """
    sub_base_user = _resolve_user_base(db, current_user)

    # normalização (obrigatórios)
    nome        = (body.nome or "").strip()
    telefone    = (body.telefone or "").strip()
    documento   = (body.documento or "").strip()

    if not documento:
        raise HTTPException(status_code=400, detail="O campo 'documento' é obrigatório.")

    # normalização (opcionais)
    rua         = _optional(body.rua)
    numero      = _optional(body.numero)
    complemento = _optional(body.complemento)
    cep         = _optional(body.cep)
    cidade      = _optional(body.cidade)
    bairro      = _optional(body.bairro)

    coletador_flag = bool(body.coletador)
    username_ent = (body.username_entregador or "").strip() if coletador_flag else None
    senha_raw = (body.senha or "").strip() if coletador_flag else None

    if coletador_flag:
        if not username_ent:
            raise HTTPException(status_code=400, detail="Informe 'username_entregador' para coletador.")
        if not senha_raw:
            raise HTTPException(status_code=400, detail="Informe 'senha' para coletador.")

        # unicidade do username
        if db.scalars(select(User).where(User.username == username_ent)).first():
            raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

    # documento único por sub_base
    exists = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base_user,
            Entregador.documento == documento,
        )
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")

    try:
        # 1️⃣ Cria ENTREGADOR
        ent = Entregador(
            sub_base=sub_base_user,
            nome=nome,
            telefone=telefone,
            documento=documento,
            ativo=True,
            rua=rua,
            numero=numero,
            complemento=complemento,
            cep=cep,
            cidade=cidade,
            bairro=bairro,
            coletador=coletador_flag,
            username_entregador=username_ent,
        )
        db.add(ent)

        # 2️⃣ Se coletador → cria USER
        if coletador_flag:
            new_user = User(
                password_hash=get_password_hash(senha_raw),
                username=username_ent,
                contato=telefone or "",
                nome=nome or None,
                status=True,
                sub_base=sub_base_user,
                coletador=True,
                username_entregador=username_ent,
                role=3,
            )
            db.add(new_user)

        db.commit()
        db.refresh(ent)
        return {"ok": True, "action": "created", "id": ent.id_entregador}

    except Exception:
        db.rollback()
        raise


@router.get("/", response_model=List[EntregadorOut])
def list_entregadores(
    status: Optional[str] = Query("todos", description="Filtrar por status: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Lista entregadores da sub_base do solicitante, filtrando por status"""
    sub_base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.sub_base == sub_base_user)
    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))
    stmt = stmt.order_by(Entregador.nome)

    return db.scalars(stmt).all()


@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Retorna um entregador específico"""
    sub_base_user = _resolve_user_base(db, current_user)
    return _get_owned_entregador(db, sub_base_user, id_entregador)


@router.patch("/{id_entregador}", response_model=EntregadorOut)
def patch_entregador(
    id_entregador: int,
    body: EntregadorUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    Atualização parcial do ENTREGADOR + promoção opcional a COLETADOR:

    - Atualiza dados do entregador.
    - username alvo = body.username_entregador (se enviado) senão o atual do entregador.
    - Procura/corrige o User correspondente. Se *não* existir e:
        (a) coletador=True no body OU o entregador já é coletador,
        (b) e existir username_alvo e senha no PATCH,
      então CRIA um User (role=3).
    - Se o User existir, sincroniza tudo.
    """
    def _optional(v):
        if v is None:
            return None
        v = v.strip()
        return v or None

    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    try:
        # =======================
        # 1) Atualiza ENTREGADOR
        # =======================
        if body.nome is not None:
            obj.nome = _optional(body.nome)

        if body.telefone is not None:
            obj.telefone = _optional(body.telefone)

        # --- documento ---
        if body.documento is not None:
            novo_doc = (body.documento or "").strip()
            if not novo_doc:
                raise HTTPException(status_code=400, detail="O campo 'documento' não pode ser vazio.")
            if novo_doc != obj.documento:
                exists = db.scalars(
                    select(Entregador).where(
                        Entregador.sub_base == sub_base_user,
                        Entregador.documento == novo_doc,
                        Entregador.id_entregador != obj.id_entregador,
                    )
                ).first()
                if exists:
                    raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")
            obj.documento = novo_doc

        # opcionais (rua, número, cep, cidade, etc)
        if body.rua is not None:         obj.rua = _optional(body.rua)
        if body.numero is not None:      obj.numero = _optional(body.numero)
        if body.complemento is not None: obj.complemento = _optional(body.complemento)
        if body.cep is not None:         obj.cep = _optional(body.cep)
        if body.cidade is not None:      obj.cidade = _optional(body.cidade)
        if body.bairro is not None:      obj.bairro = _optional(body.bairro)

        # --- ativo ---
        if body.ativo is not None:
            obj.ativo = bool(body.ativo)

        # username para vincular ao user
        username_alvo = (
            (body.username_entregador or "").strip()
            if body.username_entregador is not None
            else (obj.username_entregador or "").strip()
        ) or None

        # Se foi enviado no PATCH, grava no entregador
        if body.username_entregador is not None:
            obj.username_entregador = username_alvo

        # coletador desejado
        coletador_desejado = (
            obj.coletador if body.coletador is None else bool(body.coletador)
        )

        # ==========================
        # 2) Localiza / Cria USER
        # ==========================
        user = _find_matching_user(db, sub_base_user, username_alvo)

        deve_criar_user = (
            user is None and
            coletador_desejado is True and
            username_alvo and
            (body.senha and body.senha.strip())
        )

        if deve_criar_user:
            # unicidade global
            clash = db.scalars(select(User).where(User.username == username_alvo)).first()
            if clash:
                raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

            user = User(
                password_hash=get_password_hash(body.senha.strip()),
                username=username_alvo,
                username_entregador=username_alvo,
                sub_base=sub_base_user,
                nome=obj.nome,
                contato=obj.telefone or "",
                coletador=True,
                role=3,
                status=True,
            )
            db.add(user)

        # =============================
        # 3) Atualiza USER se existir
        # =============================
        if user is not None:
            mudou = False

            # username
            if body.username_entregador is not None:
                if username_alvo:
                    outro = db.scalars(select(User).where(User.username == username_alvo)).first()
                    if outro and outro is not user:
                        raise HTTPException(status_code=409, detail="Já existe um usuário com este username.")

                if user.username != username_alvo:
                    user.username = username_alvo
                    mudou = True
                if user.username_entregador != username_alvo:
                    user.username_entregador = username_alvo
                    mudou = True

            # coletador
            if user.coletador != coletador_desejado:
                user.coletador = coletador_desejado
                mudou = True

            # sincronizar nome/telefone (quando enviados)
            if body.nome is not None:
                nome_val = _optional(body.nome)
                if nome_val and user.nome != nome_val:
                    user.nome = nome_val
                    mudou = True

            if body.telefone is not None:
                tel_val = _optional(body.telefone)
                if tel_val and user.contato != tel_val:
                    user.contato = tel_val
                    mudou = True

            # senha
            if body.senha is not None:
                raw = body.senha.strip()
                if not raw:
                    raise HTTPException(status_code=400, detail="A nova senha não pode ser vazia.")
                user.password_hash = get_password_hash(raw)
                mudou = True

            # sempre role=3
            if mudou:
                user.role = 3

        # espelhar coletador no entregador
        if body.coletador is not None:
            obj.coletador = coletador_desejado

        db.commit()
        db.refresh(obj)
        return obj

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="Erro ao atualizar o entregador/coletador.")
@router.delete("/{id_entregador}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Remove o entregador, sem deletar o user"""
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    db.delete(obj)
    db.commit()
    return
