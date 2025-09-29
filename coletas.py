# coletas.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user  # mantém autenticação por cookie/bearer
from models import Coleta, Entregador, BasePreco, User  # nomes conforme seus models

router = APIRouter(prefix="/coletas", tags=["Coletas"])

# =========================
# Schemas
# =========================
class ColetaCreate(BaseModel):
    base: str = Field(min_length=1)                     # ex.: "3AS"
    username_entregador: str = Field(min_length=1)      # ex.: "entregador_cristian"

    # quantidades coletadas (não negativas)
    shopee: int = Field(ge=0, default=0)
    ml: int = Field(ge=0, default=0)                    # vai para coluna "mercado_livre"
    avulso: int = Field(ge=0, default=0)
    nfe: int = Field(ge=0, default=0)

    model_config = ConfigDict(from_attributes=True)


class ColetaOut(BaseModel):
    id_coleta: int
    base: str
    sub_base: str
    username_entregador: str
    shopee: int
    mercado_livre: int
    avulso: int
    nfe: int
    valor_total: Decimal

    model_config = ConfigDict(from_attributes=True)

# =========================
# Helpers
# =========================
def _decimal(v) -> Decimal:
    try:
        return Decimal(str(v or 0))
    except Exception:
        return Decimal("0")

def _resolve_user_sub_base(db: Session, current_user: User) -> str:
    user_id = getattr(current_user, "id", None)
    if user_id is not None:
        u = db.get(User, user_id)
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    email = getattr(current_user, "email", None)
    if email:
        u = db.scalars(select(User).where(User.email == email)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    uname = getattr(current_user, "username", None)
    if uname:
        u = db.scalars(select(User).where(User.username == uname)).first()
        if u and getattr(u, "sub_base", None):
            return u.sub_base
    raise HTTPException(status_code=400, detail="sub_base não definida para o usuário em 'users'.")

def _get_owned_coleta(db: Session, sub_base_user: str, id_coleta: int) -> Coleta:
    obj = db.get(Coleta, id_coleta)
    if not obj or obj.sub_base != sub_base_user:
        raise HTTPException(status_code=404, detail="Não encontrado")
    return obj

# =========================
# POST /coletas  (já existia)
# =========================
@router.post("/", status_code=status.HTTP_201_CREATED, response_model=ColetaOut)
def criar_coleta(
    body: ColetaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Fluxo:
      1) Recebe quantidades + base + username_entregador
      2) Encontra o ENTREGADOR por username_entregador e lê sua sub_base
      3) Busca a tabela de preços da BASE (BasePreco) por (sub_base, base)
      4) Calcula valor_total = soma(qtd * preço)
      5) Persiste em COLETAS mapeando 'ml' -> coluna 'mercado_livre'
    """

    # (2) Resolver entregador -> sub_base
    ent = db.scalars(
        select(Entregador).where(Entregador.username_entregador == body.username_entregador)
    ).first()

    if not ent:
        raise HTTPException(
            status_code=404,
            detail=f"Entregador com username '{body.username_entregador}' não encontrado."
        )

    if hasattr(Entregador, "sub_base"):
        sub_base = ent.sub_base
    else:
        sub_base = getattr(ent, "base", None)

    if not sub_base:
        raise HTTPException(
            status_code=422,
            detail="Entregador sem 'sub_base' definida."
        )

    # (3) Buscar preços na tabela Base (BasePreco) por (sub_base, base)
    precos = db.scalars(
        select(BasePreco).where(
            BasePreco.sub_base == sub_base,
            BasePreco.base == body.base,
        )
    ).first()

    if not precos:
        raise HTTPException(
            status_code=404,
            detail=f"Tabela de preços não encontrada para sub_base='{sub_base}' e base='{body.base}'."
        )

    # (4) Calcular total com Decimal (2 casas)
    p_shopee = _decimal(precos.shopee)
    p_ml     = _decimal(precos.ml)
    p_avulso = _decimal(precos.avulso)
    p_nfe    = _decimal(precos.nfe)

    total = (
        _decimal(body.shopee) * p_shopee +
        _decimal(body.ml)     * p_ml +
        _decimal(body.avulso) * p_avulso +
        _decimal(body.nfe)    * p_nfe
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # (5) Inserir na tabela COLETAS (ml -> mercado_livre)
    row = Coleta(
        sub_base=sub_base,
        base=body.base,
        username_entregador=body.username_entregador,
        shopee=body.shopee,
        mercado_livre=body.ml,
        avulso=body.avulso,
        nfe=body.nfe,
        valor_total=total,
    )

    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Falha ao gravar coleta: {e}")

    return row

# =========================
# (NOVO) UPDATE SCHEMA (parcial) para PATCH
# =========================
class ColetaUpdate(BaseModel):
    base: Optional[str] = None
    username_entregador: Optional[str] = None
    shopee: Optional[int] = Field(default=None, ge=0)
    ml: Optional[int]     = Field(default=None, ge=0)  # mapeado para 'mercado_livre'
    avulso: Optional[int] = Field(default=None, ge=0)
    nfe: Optional[int]    = Field(default=None, ge=0)
    model_config = ConfigDict(from_attributes=True)

# =========================
# (NOVO) GET /coletas/  -> listar (escopo por sub_base do usuário)
# =========================
@router.get("/", response_model=List[ColetaOut])
def list_coletas(
    base: Optional[str] = Query(None, description="Filtra por base ex.: '3AS'"),
    username_entregador: Optional[str] = Query(None, description="Filtra por username do entregador"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)

    stmt = select(Coleta).where(Coleta.sub_base == sub_base_user)
    if base:
        stmt = stmt.where(Coleta.base == base.strip())
    if username_entregador:
        stmt = stmt.where(Coleta.username_entregador == username_entregador.strip())

    stmt = stmt.order_by(Coleta.id_coleta.desc())
    rows = db.scalars(stmt).all()
    return rows

# =========================
# (NOVO) GET /coletas/{id_coleta}  -> detalhe
# =========================
@router.get("/{id_coleta}", response_model=ColetaOut)
def get_coleta(
    id_coleta: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = _get_owned_coleta(db, sub_base_user, id_coleta)
    return obj

# =========================
# (NOVO) PATCH /coletas/{id_coleta}  -> atualização parcial + recálculo
# =========================
@router.patch("/{id_coleta}", response_model=ColetaOut)
def patch_coleta(
    id_coleta: int,
    body: ColetaUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Regras:
      - Permite atualizar base, username_entregador e quantidades.
      - Sempre recalcula 'valor_total' com a tabela de preços vigente (BasePreco) para (sub_base, base).
      - sub_base é a do entregador (se alterado) ou a atual da coleta (se entregador não mudar).
    """
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = _get_owned_coleta(db, sub_base_user, id_coleta)

    # Determinar novos campos (sem gravar ainda)
    new_base = obj.base
    new_username = obj.username_entregador

    if body.base is not None:
        nb = (body.base or "").strip()
        if not nb:
            raise HTTPException(status_code=400, detail="O campo 'base' não pode ficar vazio.")
        new_base = nb

    if body.username_entregador is not None:
        nu = (body.username_entregador or "").strip()
        if not nu:
            raise HTTPException(status_code=400, detail="O campo 'username_entregador' não pode ficar vazio.")
        new_username = nu

    # Resolver sub_base (se username_entregador mudar, pega a sub_base do novo entregador)
    new_sub_base = obj.sub_base
    if new_username != obj.username_entregador:
        ent = db.scalars(
            select(Entregador).where(Entregador.username_entregador == new_username)
        ).first()
        if not ent:
            raise HTTPException(status_code=404, detail=f"Entregador com username '{new_username}' não encontrado.")
        new_sub_base = getattr(ent, "sub_base", None) or getattr(ent, "base", None)
        if not new_sub_base:
            raise HTTPException(status_code=422, detail="Entregador sem 'sub_base' definida.")

    # Quantidades (se não enviadas, mantém as atuais)
    q_shopee = obj.shopee if body.shopee is None else int(body.shopee)
    q_ml     = obj.mercado_livre if body.ml is None else int(body.ml)
    q_avulso = obj.avulso if body.avulso is None else int(body.avulso)
    q_nfe    = obj.nfe if body.nfe is None else int(body.nfe)

    # Buscar preços BasePreco para (new_sub_base, new_base)
    precos = db.scalars(
        select(BasePreco).where(
            BasePreco.sub_base == new_sub_base,
            BasePreco.base == new_base,
        )
    ).first()
    if not precos:
        raise HTTPException(
            status_code=404,
            detail=f"Tabela de preços não encontrada para sub_base='{new_sub_base}' e base='{new_base}'."
        )

    p_shopee = _decimal(precos.shopee)
    p_ml     = _decimal(precos.ml)
    p_avulso = _decimal(precos.avulso)
    p_nfe    = _decimal(precos.nfe)

    new_total = (
        _decimal(q_shopee) * p_shopee +
        _decimal(q_ml)     * p_ml +
        _decimal(q_avulso) * p_avulso +
        _decimal(q_nfe)    * p_nfe
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Persistir mudanças
    obj.base = new_base
    obj.username_entregador = new_username
    obj.sub_base = new_sub_base
    obj.shopee = q_shopee
    obj.mercado_livre = q_ml
    obj.avulso = q_avulso
    obj.nfe = q_nfe
    obj.valor_total = new_total

    db.commit()
    db.refresh(obj)
    return obj

# =========================
# (NOVO) DELETE /coletas/{id_coleta}
# =========================
@router.delete("/{id_coleta}", status_code=status.HTTP_204_NO_CONTENT)
def delete_coleta(
    id_coleta: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base_user = _resolve_user_sub_base(db, current_user)
    obj = _get_owned_coleta(db, sub_base_user, id_coleta)
    db.delete(obj)
    db.commit()
    return
