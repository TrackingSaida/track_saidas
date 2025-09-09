# saidas_routes.py
from __future__ import annotations

from typing import Optional, List, Literal
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Column, BigInteger, Text, Date, DateTime, Numeric, func
from sqlalchemy.orm import Session

from db import Base, get_db
from models import User                   # já existente
from auth import get_current_user         # já existente

router = APIRouter(prefix="/saidas", tags=["Saídas"])

# ---------- MODELOS ORM ----------
class Owner(Base):
    __tablename__ = "owner"
    id_owner    = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    email       = Column(Text, nullable=True)
    username    = Column(Text, nullable=True)
    cobranca    = Column(Text, nullable=True)           # '0' ou '1'
    valor       = Column(Numeric(12, 2), nullable=True)
    mensalidade = Column(Date, nullable=True)
    creditos    = Column(Numeric(12, 2), nullable=True) # saldo pré-pago
    base        = Column(Text, nullable=True)
    contato     = Column(Text, nullable=True)

class Saida(Base):
    __tablename__ = "saidas"
    id_saida  = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=False), server_default=func.now())
    data      = Column(Date, server_default=func.current_date())
    base      = Column(Text, nullable=True)
    username  = Column(Text, nullable=True)
    entregador= Column(Text, nullable=True)
    codigo    = Column(Text, nullable=True)
    servico   = Column(Text, nullable=True)
    status    = Column(Text, nullable=True)

# ---------- SCHEMAS ----------
class SaidaCreate(BaseModel):
    entregador: str = Field(min_length=1)
    codigo: Optional[str] = None
    codigos: Optional[List[str]] = None
    servico: Optional[str] = None

    @property
    def lista_codigos(self) -> List[str]:
        if self.codigos:
            return [c.strip() for c in self.codigos if c and c.strip()]
        if self.codigo and self.codigo.strip():
            return [self.codigo.strip()]
        return []

class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    base: Optional[str]
    username: Optional[str]
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    model_config = ConfigDict(from_attributes=True)

class ResumoBatchOut(BaseModel):
    base: str
    username: str
    entregador: str
    servico: str
    qtd_codigos: int
    codigos_inseridos: List[str]
    modo_cobranca: Literal[0, 1]
    saldo_restante: Optional[float] = None

# ---------- ENDPOINT ----------
@router.post("/registrar", status_code=status.HTTP_201_CREATED)
def registrar_saida(
    payload: SaidaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1) do token
    username = getattr(current_user, "username", None)
    email    = getattr(current_user, "email", None)

    # 2) base via users
    base_user = getattr(current_user, "base", None)
    if not base_user:
        # se o modelo User não carrega base via token, faça uma query por id/email e pegue a base
        u = db.query(User).filter(
            (User.email == email) | (User.username == username)
        ).first()
        base_user = getattr(u, "base", None) if u else None

    if not base_user or not username:
        raise HTTPException(status_code=401, detail="Usuário sem 'base' ou 'username'.")

    # 3) parâmetros em owner (prioriza base; fallback por email/username)
    conta = (
        db.query(Owner)
          .filter(Owner.base == base_user)
          .first()
    )
    if not conta:
        conta = (
            db.query(Owner)
              .filter( (Owner.email == email) | (Owner.username == username) )
              .first()
        )
    if not conta:
        raise HTTPException(status_code=404, detail="Owner não encontrado para esta base/usuário.")

    # normalizações
    try:
        cobranca = int(str(conta.cobranca or "0"))
    except Exception:
        cobranca = 0

    valor       = float(conta.valor or 0)
    creditos    = float(conta.creditos or 0)
    mensalidade = conta.mensalidade

    codigos    = payload.lista_codigos
    if not codigos:
        raise HTTPException(status_code=422, detail="Informe 'codigo' ou 'codigos'.")
    entregador = payload.entregador.strip()
    servico    = (payload.servico or "padrao").strip()
    qtd        = len(codigos)

    # 4–5) cobrança + inserts (uma commit no final; sem nested transactions)
    rows: List[Saida] = []
    try:
        # cobrança em OWNER
        if cobranca == 0:
            custo = round(valor * qtd, 2)
            if creditos < custo:
                raise HTTPException(status_code=409,
                    detail=f"Créditos insuficientes. Necessário {custo:.2f}, saldo {creditos:.2f}.")
            # debita
            conta.creditos = round(creditos - custo, 2)
            db.add(conta)

        elif cobranca == 1:
            if not mensalidade or date.today() > mensalidade:
                raise HTTPException(status_code=402, detail="Mensalidade vencida ou não configurada.")
        else:
            raise HTTPException(status_code=422, detail="Valor inválido em 'cobranca' (use 0 ou 1).")

        # inserts em SAIDAS
        for codigo in codigos:
            if not codigo:
                continue
            row = Saida(
                base=base_user,
                username=username,
                entregador=entregador,
                codigo=codigo,
                servico=servico,
                status="saiu",
            )
            db.add(row)
            rows.append(row)

        db.commit()

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao registrar saídas: {e}")

    # 6) resposta
    if len(rows) == 1:
        db.refresh(rows[0])
        return SaidaOut.model_validate(rows[0])

    saldo_restante = float(conta.creditos or 0.0) if cobranca == 0 else None
    return ResumoBatchOut(
        base=base_user,
        username=username,
        entregador=entregador,
        servico=servico,
        qtd_codigos=len(rows),
        codigos_inseridos=[r.codigo for r in rows],
        modo_cobranca=cobranca,
        saldo_restante=saldo_restante,
    )
