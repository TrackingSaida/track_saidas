from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy import select

from db import get_db
from auth import get_current_user
from models import Owner, User, OwnerCobrancaItem, BaseSellerDados

router = APIRouter(prefix="/owner", tags=["Owner"])

# ============================================================
# SCHEMAS
# ============================================================

def _normalize_tipo_owner(value: Optional[str]) -> str:
    v = (value or "subbase").strip().lower()
    if v not in ("base", "subbase"):
        return "subbase"
    return v


class OwnerCreate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    valor: Optional[float] = Field(default=None)
    sub_base: Optional[str] = None
    contato: Optional[str] = None
    teste: Optional[bool] = None
    modo_operacao: Optional[str] = None
    tipo_owner: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerUpdate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    valor: Optional[float] = None
    contato: Optional[str] = None
    nome_fantasia: Optional[str] = None
    ativo: Optional[bool] = None
    ignorar_coleta: Optional[bool] = None
    teste: Optional[bool] = None
    modo_operacao: Optional[str] = None
    tipo_owner: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OwnerOut(BaseModel):
    id_owner: int
    email: Optional[str]
    username: Optional[str]
    valor: Optional[float]
    nome_fantasia: Optional[str] = None
    sub_base: Optional[str]
    contato: Optional[str]
    ativo: bool
    ignorar_coleta: bool
    teste: bool
    modo_operacao: Optional[str] = None
    tipo_owner: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ============================================================
# HELPERS
# ============================================================

def _get_owner_by_sub_base(db: Session, sub_base: str) -> Optional[Owner]:
    return db.scalar(select(Owner).where(Owner.sub_base == sub_base))


# ============================================================
# CREATE OWNER
# ============================================================

@router.post("/", status_code=201)
def create_owner(
    body: OwnerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    email = body.email or current_user.email
    username = body.username or current_user.username

    if not body.sub_base:
        raise HTTPException(422, "sub_base é obrigatória.")

    exists = db.scalar(select(Owner).where(Owner.sub_base == body.sub_base))
    if exists:
        raise HTTPException(409, "Já existe um Owner para esta sub_base.")

    # Na criação, ignorar_coleta é sempre False — só modo codigo permitido
    ignorar_coleta = False
    modo_operacao = body.modo_operacao if body.modo_operacao is not None else "codigo"
    if modo_operacao in ("saida", "coleta_manual"):
        raise HTTPException(
            400,
            "Modos 'saida' e 'coleta_manual' exigem 'Ignorar Coleta' ativo. Configure após criar o owner."
        )

    tipo_owner = _normalize_tipo_owner(body.tipo_owner)

    obj = Owner(
        email=email,
        username=username,
        valor=body.valor or 0.0,
        sub_base=body.sub_base,
        contato=body.contato,
        ativo=True,
        ignorar_coleta=ignorar_coleta,
        teste=bool(body.teste) if body.teste is not None else False,
        modo_operacao=modo_operacao,
        tipo_owner=tipo_owner,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)

    return {"ok": True, "id_owner": obj.id_owner}


# ============================================================
# GET /owner/me
# ============================================================

@router.get("/me", response_model=OwnerOut)
def get_owner_for_current_user(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.sub_base:
        raise HTTPException(404, "Usuário não possui sub_base associada.")

    owner = _get_owner_by_sub_base(db, current_user.sub_base)
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")

    return owner


# ============================================================
# LISTAR TODOS (ADMIN)
# ============================================================

@router.get("/", response_model=List[OwnerOut])
def list_owners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 0:
        raise HTTPException(403, "Acesso restrito ao administrador.")

    return db.scalars(select(Owner)).all()


# ============================================================
# UPDATE (PATCH ÚNICO)
# ============================================================

@router.patch("/{id_owner}", response_model=OwnerOut)
def update_owner(
    id_owner: int,
    body: OwnerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")

    if current_user.role != 0:
        raise HTTPException(403, "Apenas administradores podem editar Owner.")

    # Campos editáveis
    if body.email is not None:
        owner.email = body.email

    if body.username is not None:
        owner.username = body.username

    if body.valor is not None:
        owner.valor = body.valor

    if body.contato is not None:
        owner.contato = body.contato

    if body.nome_fantasia is not None:
        owner.nome_fantasia = (body.nome_fantasia or "").strip() or None

    # 🔥 Campos adicionados agora
    if body.ativo is not None:
        owner.ativo = body.ativo

    if body.ignorar_coleta is not None:
        owner.ignorar_coleta = body.ignorar_coleta
        if not body.ignorar_coleta and owner.modo_operacao in ("saida", "coleta_manual"):
            owner.modo_operacao = "codigo"

    if body.teste is not None:
        owner.teste = body.teste

    if body.modo_operacao is not None:
        ign = body.ignorar_coleta if body.ignorar_coleta is not None else owner.ignorar_coleta
        if body.modo_operacao in ("saida", "coleta_manual") and not ign:
            raise HTTPException(
                400,
                "Para usar modo 'saida' ou 'coleta_manual', o campo 'Ignorar Coleta' deve estar ativo."
            )
        if body.modo_operacao == "codigo" and ign:
            raise HTTPException(
                400,
                "Modo 'codigo' requer 'Ignorar Coleta' desativado."
            )
        owner.modo_operacao = body.modo_operacao

    if body.tipo_owner is not None:
        owner.tipo_owner = _normalize_tipo_owner(body.tipo_owner)

    db.commit()
    db.refresh(owner)
    return owner


# ============================================================
# DADOS DO SELLER (CNPJ/ENDEREÇO) POR OWNER
# ============================================================


class SellerDadosBase(BaseModel):
    cnpj: Optional[str] = None
    rua: Optional[str] = None
    numero: Optional[str] = None
    complemento: Optional[str] = None
    bairro: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    cep: Optional[str] = None
    base_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class SellerDadosOut(SellerDadosBase):
    id_seller: int
    owner_id: int


@router.get("/{id_owner}/seller-dados", response_model=SellerDadosOut)
def get_seller_dados(
    id_owner: int,
    base_id: Optional[int] = Query(default=None, description="Filtrar dados do seller por id_base associado"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    stmt = select(BaseSellerDados).where(BaseSellerDados.owner_id == id_owner)
    if base_id is not None:
        stmt = stmt.where(BaseSellerDados.base_id == base_id)

    seller = db.scalar(stmt)
    if not seller:
        raise HTTPException(404, "Dados de seller não encontrados para este owner.")
    return seller


@router.patch("/{id_owner}/seller-dados", response_model=SellerDadosOut)
def upsert_seller_dados(
    id_owner: int,
    body: SellerDadosBase,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (0, 1):
        raise HTTPException(403, "Acesso negado.")

    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404, "Owner não encontrado.")

    data = body.model_dump(exclude_unset=True)
    base_id = data.get("base_id")

    # Buscar por (owner_id, base_id) para permitir um registro de seller por base
    stmt = select(BaseSellerDados).where(BaseSellerDados.owner_id == id_owner)
    if base_id is not None:
        stmt = stmt.where(BaseSellerDados.base_id == base_id)
    seller = db.scalar(stmt)

    if not seller:
        # criação exige CNPJ e endereço mínimo
        cnpj = (data.get("cnpj") or "").strip()
        rua = (data.get("rua") or "").strip()
        numero = (data.get("numero") or "").strip()
        bairro = (data.get("bairro") or "").strip()
        cidade = (data.get("cidade") or "").strip()
        cep = (data.get("cep") or "").strip()
        if not all([cnpj, rua, numero, bairro, cidade, cep]):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Campos obrigatórios para criar seller: cnpj, rua, numero, bairro, cidade, cep.",
            )

        seller = BaseSellerDados(
            owner_id=id_owner,
            base_id=base_id,
            cnpj=cnpj,
            rua=rua,
            numero=numero,
            complemento=(data.get("complemento") or "").strip() or None,
            bairro=bairro,
            cidade=cidade,
            estado=(data.get("estado") or "").strip() or None,
            cep=cep,
        )
        db.add(seller)

    else:
        # atualização parcial
        if "base_id" in data:
            seller.base_id = data["base_id"]
        if "cnpj" in data:
            seller.cnpj = (data["cnpj"] or "").strip() or seller.cnpj
        if "rua" in data:
            seller.rua = (data["rua"] or "").strip() or seller.rua
        if "numero" in data:
            seller.numero = (data["numero"] or "").strip() or seller.numero
        if "complemento" in data:
            seller.complemento = (data["complemento"] or "").strip() or None
        if "bairro" in data:
            seller.bairro = (data["bairro"] or "").strip() or seller.bairro
        if "cidade" in data:
            seller.cidade = (data["cidade"] or "").strip() or seller.cidade
        if "estado" in data:
            seller.estado = (data["estado"] or "").strip() or None
        if "cep" in data:
            seller.cep = (data["cep"] or "").strip() or seller.cep

    db.commit()
    db.refresh(seller)
    return seller


# ============================================================
# ENDPOINTS DE ATIVAR/DESATIVAR (opcionais)
# ============================================================

@router.patch("/{id_owner}/ativar")
def ativar_owner(id_owner: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404)
    if current_user.role != 0:
        raise HTTPException(403)
    owner.ativo = True
    db.commit()
    return {"ok": True}


@router.patch("/{id_owner}/desativar")
def desativar_owner(id_owner: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    owner = db.get(Owner, id_owner)
    if not owner:
        raise HTTPException(404)
    if current_user.role != 0:
        raise HTTPException(403)
    owner.ativo = False
    db.commit()
    return {"ok": True}
