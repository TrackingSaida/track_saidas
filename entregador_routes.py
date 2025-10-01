# entregador_routes.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import date

from db import get_db
from auth import get_current_user, get_password_hash
from models import User, Entregador

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================
# SCHEMAS (Pydantic)
# =========================
class EntregadorCreate(BaseModel):
    # obrigatórios básicos
    nome: str = Field(min_length=1)
    telefone: str = Field(min_length=1)
    documento: str = Field(min_length=1)

    # endereço
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: str = Field(min_length=1)
    cep: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    bairro: str = Field(min_length=1)

    # novos campos
    coletador: bool = False
    username_entregador: Optional[str] = None
    senha_entregador: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EntregadorUpdate(BaseModel):
    # atualização parcial (envie só o que quer alterar)
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None

    # endereço
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None

    # novos campos
    coletador: Optional[bool] = None
    username_entregador: Optional[str] = None
    senha_entregador: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EntregadorOut(BaseModel):
    id_entregador: int
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    ativo: bool
    data_cadastro: Optional[date] = None

    # endereço
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None

    # novos campos (não expõe senha)
    coletador: bool
    username_entregador: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# =========================
# HELPERS
# =========================
def _resolve_user_base(db: Session, current_user) -> str:
    # 1) por ID
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    # 2) por email
    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    # 3) por username
    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário em 'users'.")

def _get_owned_entregador(db: Session, sub_base_user: str, id_entregador: int) -> Entregador:
    obj = db.get(Entregador, id_entregador)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj

def _check_username_duplicado(
    db: Session, sub_base_user: str, username: str, ignorar_id: Optional[int] = None
) -> None:
    if not username:
        return
    stmt = select(Entregador).where(
        Entregador.sub_base == sub_base_user,
        Entregador.username_entregador == username
    )
    if ignorar_id is not None:
        stmt = stmt.where(Entregador.id_entregador != ignorar_id)
    if db.scalars(stmt).first():
        raise HTTPException(status_code=409, detail="Já existe um entregador com este username nesta sub_base.")

# =========================
# ROTAS
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_entregador(
    body: EntregadorCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)

    # normaliza/valida
    nome        = (body.nome or "").strip()
    telefone    = (body.telefone or "").strip()
    documento   = (body.documento or "").strip()
    rua         = (body.rua or "").strip()
    numero      = (body.numero or "").strip()
    complemento = (body.complemento or "").strip()
    cep         = (body.cep or "").strip()
    cidade      = (body.cidade or "").strip()
    bairro      = (body.bairro or "").strip()

    coletador   = bool(body.coletador)
    username    = (body.username_entregador or "").strip() or None
    senha_plain = (body.senha_entregador or "").strip() or None

    if not documento:
        raise HTTPException(status_code=400, detail="O campo 'documento' é obrigatório.")

    # duplicidade de documento por sub_base
    exists = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base_user,
            Entregador.documento == documento,
        )
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")

    # regra: se for coletador, exige username e senha
    if coletador:
        if not username or not senha_plain:
            raise HTTPException(
                status_code=400,
                detail="Para coletador=true é obrigatório informar 'username_entregador' e 'senha_entregador'."
            )
        _check_username_duplicado(db, sub_base_user, username)

    senha_hash = get_password_hash(senha_plain) if (coletador and senha_plain) else None

    obj = Entregador(
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
        # novos campos
        coletador=coletador,
        username_entregador=username,
        senha_entregador=senha_hash,
        # data_cadastro: DEFAULT CURRENT_DATE no banco
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return {"ok": True, "action": "created", "id": obj.id_entregador}

@router.get("/", response_model=List[EntregadorOut])
def list_entregadores(
    status: Optional[str] = Query("todos", description="Filtrar por status: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.sub_base == sub_base_user)

    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))

    stmt = stmt.order_by(Entregador.nome)
    rows = db.scalars(stmt).all()
    return rows

@router.get("/{id_entregador}", response_model=EntregadorOut)
def get_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    return obj

@router.patch("/{id_entregador}", response_model=EntregadorOut)
def patch_entregador(
    id_entregador: int,
    body: EntregadorUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)

    # básicos
    if body.nome is not None:
        obj.nome = (body.nome or "").strip()
    if body.telefone is not None:
        obj.telefone = (body.telefone or "").strip()
    if body.documento is not None:
        novo_doc = (body.documento or "").strip()
        if not novo_doc:
            raise HTTPException(status_code=400, detail="O campo 'documento' não pode ficar vazio.")
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

    # endereço
    if body.rua is not None:
        obj.rua = (body.rua or "").strip()
    if body.numero is not None:
        obj.numero = (body.numero or "").strip()
    if body.complemento is not None:
        obj.complemento = (body.complemento or "").strip()
    if body.cep is not None:
        obj.cep = (body.cep or "").strip()
    if body.cidade is not None:
        obj.cidade = (body.cidade or "").strip()
    if body.bairro is not None:
        obj.bairro = (body.bairro or "").strip()

    # novos campos
    username = (body.username_entregador or "").strip() if body.username_entregador is not None else None
    if username is not None:
        if username == "":
            obj.username_entregador = None
        else:
            _check_username_duplicado(db, sub_base_user, username, ignorar_id=obj.id_entregador)
            obj.username_entregador = username

    if body.senha_entregador is not None:
        senha_plain = (body.senha_entregador or "").strip()
        if senha_plain == "":
            obj.senha_entregador = None
        else:
            obj.senha_entregador = get_password_hash(senha_plain)

    if body.coletador is not None:
        will_be_coletador = bool(body.coletador)
        if will_be_coletador and not (obj.username_entregador or username):
            # se vai virar coletador e ainda não tem username definido
            raise HTTPException(
                status_code=400,
                detail="Para definir coletador=true, informe 'username_entregador' (e opcionalmente uma nova senha)."
            )
        obj.coletador = will_be_coletador

    db.commit()
    db.refresh(obj)
    return obj

@router.delete("/{id_entregador}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador(
    id_entregador: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    sub_base_user = _resolve_user_base(db, current_user)
    obj = _get_owned_entregador(db, sub_base_user, id_entregador)
    db.delete(obj)
    db.commit()
    return
