# coletas.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user  # mantém autenticação por cookie/bearer
from models import Coleta, Entregador, BasePreco, User  # <-- nomes conforme seus models

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

# =========================
# POST /coletas
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
        # Se seu model ainda tiver 'base' como a sub_base (legado), remova essa parte quando migrar totalmente
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
