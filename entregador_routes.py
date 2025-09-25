# entregador_routes.py
from __future__ import annotations
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from datetime import date

from db import get_db
from auth import get_current_user
from models import User, Entregador

router = APIRouter(prefix="/entregadores", tags=["Entregadores"])

# =========================
# SCHEMAS (Pydantic)
# =========================
class EntregadorCreate(BaseModel):
    nome: str = Field(min_length=1)
    telefone: str = Field(min_length=1)
    documento: str = Field(min_length=1)  # <- obrigatório (não aceita vazio)
    model_config = ConfigDict(from_attributes=True)
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: str = Field(min_length=1)
    cep: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    bairro: str = Field(min_length=1)


class EntregadorUpdate(BaseModel):
    # atualização parcial (envie só o que quer alterar)
    nome: Optional[str] = None
    telefone: Optional[str] = None
    documento: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    cep: Optional[str] = None
    cidade: Optional[str] = None
    bairro: Optional[str] = None
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

    if not documento:
        raise HTTPException(status_code=400, detail="O campo 'documento' é obrigatório.")

    # (opcional) checar duplicidade de documento por sub_base
    exists = db.scalars(
        select(Entregador).where(
            Entregador.sub_base == sub_base_user,
            Entregador.documento == documento,
        )
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="Já existe um entregador com esse documento nesta sub_base.")

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

    if body.nome is not None:
        obj.nome = body.nome.strip()
    if body.telefone is not None:
        obj.telefone = body.telefone.strip()
    if body.documento is not None:
        novo_doc = body.documento.strip()
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

    # NOVOS CAMPOS
    if body.rua is not None:
        obj.rua = body.rua.strip()
    if body.numero is not None:
        obj.numero = body.numero.strip()
    if body.complemento is not None:
        obj.complemento = body.complemento.strip()
    if body.cep is not None:
        obj.cep = body.cep.strip()
    if body.cidade is not None:
        obj.cidade = body.cidade.strip()
    if body.bairro is not None:
        obj.bairro = body.bairro.strip()

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
