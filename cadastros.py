from __future__ import annotations

# ----------------------------------------------------------------------
# Rotas de CADASTROS (somente campos "vermelhos" + id)
#
# • users:       senha, valor (R$), mensalidade, creditos
# • entregador:  nome, telefone
# • estacao:     estacao
#
# A API faz "upsert por ID": se o registro existir, atualiza;
# se não existir, cria um novo com o ID informado e só os campos permitidos.
# Os demais campos permanecem intocados (serão ajustados manualmente).
# ----------------------------------------------------------------------

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ConfigDict

from sqlalchemy import Column, Integer, Text, Date as SA_Date
from sqlalchemy.orm import Session

# Importa a Base e a sessão do seu app principal (sem import relativo)
from main import Base, get_db

router = APIRouter(prefix="/cadastros", tags=["Cadastros"])

# ======================================================================
# MODELOS SQLALCHEMY (espelham suas tabelas existentes)
# IMPORTANTe: colunas NÃO "vermelhas" ficam como nullable=True aqui
# para não travar quando criarmos linhas só com parte dos campos.
# ======================================================================

class User(Base):
    __tablename__ = "users"

    id          = Column(Integer, primary_key=True, autoincrement=False)  # vamos aceitar ID informado
    email       = Column(Text, nullable=True)
    senha       = Column(Text, nullable=True)            # << vermelho
    username    = Column(Text, nullable=True)
    contato     = Column(Text, nullable=True)
    status      = Column(Text, nullable=True)
    cobranca    = Column(Text, nullable=True)
    valor       = Column(Text, nullable=True)            # << "R$" (vermelho)
    mensalidade = Column(SA_Date, nullable=True)         # << vermelho (data)
    creditos    = Column(Text, nullable=True)            # << vermelho


class Entregador(Base):
    __tablename__ = "entregador"

    id         = Column(Integer, primary_key=True, autoincrement=False)  # aceitar ID informado
    email_base = Column(Text, nullable=True)
    nome       = Column(Text, nullable=True)           # << vermelho
    telefone   = Column(Text, nullable=True)           # << vermelho


class Estacao(Base):
    __tablename__ = "estacao"

    id         = Column(Integer, primary_key=True, autoincrement=False)  # aceitar ID informado
    email_base = Column(Text, nullable=True)
    estacao    = Column(Text, nullable=True)           # << vermelho (se quiser como int, mude para Integer)


# ======================================================================
# SCHEMAS (apenas campos "vermelhos")
# Uso tipos Python. Para data aceito str e converto mais abaixo.
# ======================================================================

class UserFields(BaseModel):
    senha: Optional[str] = None
    valor: Optional[str] = None                     # coluna "R$"
    mensalidade: Optional[str] = Field(             # "YYYY-MM-DD" ou "DD/MM/YYYY"
        default=None,
        description='Data em "YYYY-MM-DD" ou "DD/MM/YYYY".'
    )
    creditos: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EntregadorFields(BaseModel):
    nome: Optional[str] = None
    telefone: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EstacaoFields(BaseModel):
    estacao: Optional[str] = None                   # se quiser número, troque para Optional[int]

    model_config = ConfigDict(from_attributes=True)


# ======================================================================
# Utils
# ======================================================================

def _parse_date_maybe(value: Optional[str]) -> Optional[date]:
    """Aceita 'YYYY-MM-DD' ou 'DD/MM/YYYY'. Retorna date ou None."""
    if not value:
        return None
    s = value.strip()
    # tenta ISO
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    # tenta BR
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Formato de data inválido para mensalidade: '{value}'. Use 'YYYY-MM-DD' ou 'DD/MM/YYYY'.",
        )


# ======================================================================
# ENDPOINTS - apenas upsert por ID (cria/atualiza os campos "vermelhos")
# URL final (no Render): /api/cadastros/...
# ======================================================================

@router.post("/users/{id}", status_code=status.HTTP_200_OK)
def upsert_user(id: int, body: UserFields, db: Session = Depends(get_db)):
    obj = db.get(User, id)
    created = False
    if obj is None:
        obj = User(id=id)
        db.add(obj)
        created = True

    if body.senha is not None:
        obj.senha = body.senha
    if body.valor is not None:
        obj.valor = body.valor
    if body.mensalidade is not None:
        obj.mensalidade = _parse_date_maybe(body.mensalidade)
    if body.creditos is not None:
        obj.creditos = body.creditos

    db.commit()
    db.refresh(obj)
    return {
        "ok": True,
        "action": "created" if created else "updated",
        "id": obj.id,
    }


@router.post("/entregadores/{id}", status_code=status.HTTP_200_OK)
def upsert_entregador(id: int, body: EntregadorFields, db: Session = Depends(get_db)):
    obj = db.get(Entregador, id)
    created = False
    if obj is None:
        obj = Entregador(id=id)
        db.add(obj)
        created = True

    if body.nome is not None:
        obj.nome = body.nome
    if body.telefone is not None:
        obj.telefone = body.telefone

    db.commit()
    db.refresh(obj)
    return {
        "ok": True,
        "action": "created" if created else "updated",
        "id": obj.id,
    }


@router.post("/estacoes/{id}", status_code=status.HTTP_200_OK)
def upsert_estacao(id: int, body: EstacaoFields, db: Session = Depends(get_db)):
    obj = db.get(Estacao, id)
    created = False
    if obj is None:
        obj = Estacao(id=id)
        db.add(obj)
        created = True

    if body.estacao is not None:
        obj.estacao = str(body.estacao)  # guarda como texto; mude para int se a coluna for Integer

    db.commit()
    db.refresh(obj)
    return {
        "ok": True,
        "action": "created" if created else "updated",
        "id": obj.id,
    }
