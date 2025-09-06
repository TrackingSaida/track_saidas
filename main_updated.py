from __future__ import annotations

import os
from datetime import datetime, date
from typing import Optional

from fastapi import FastAPI, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import Column, BigInteger, Text, Date, DateTime, func
from sqlalchemy.orm import Session

# DB centralizado
from db import Base, get_db
from models import User  # para o type hint do endpoint protegido

# Config
API_PREFIX = os.getenv("API_PREFIX", "/api")

# Modelo: tabela saidas
class Saida(Base):
    __tablename__ = "saidas"

    id_saida = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=False), server_default=func.now())
    data = Column(Date, server_default=func.current_date())

    base = Column(Text, nullable=True)
    entregador = Column(Text, nullable=True)
    codigo = Column(Text, nullable=True)
    servico = Column(Text, nullable=True)
    status = Column(Text, nullable=True)
    estacao = Column(Text, nullable=True)


# Schemas
class SaidaCreate(BaseModel):
    base: str = Field(min_length=1)
    entregador: str = Field(min_length=1)
    estacao: str = Field(min_length=1)
    codigo: str = Field(min_length=1)
    servico: Optional[str] = None


class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    base: Optional[str] = None
    entregador: Optional[str] = None
    codigo: Optional[str] = None
    servico: Optional[str] = None
    status: Optional[str] = None
    estacao: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# App
app = FastAPI(title="API Saídas", version="0.2.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rotas externas
from users_routes_updated import router as users_router  # noqa: E402
from entregador_routes import router as entregadores_router  # noqa: E402
from estacao_routes import router as estacoes_router  # noqa: E402
from auth import router as auth_router, get_current_user  # noqa: E402

app.include_router(users_router, prefix=API_PREFIX)
app.include_router(entregadores_router, prefix=API_PREFIX)
app.include_router(estacoes_router, prefix=API_PREFIX)
app.include_router(auth_router, prefix=API_PREFIX)


# Healthcheck
@app.get(f"{API_PREFIX}/health")
def health():
    return {"status": "ok"}


# Endpoint: registrar saída (protegido por JWT)
@app.post(
    f"{API_PREFIX}/saidas/registrar",
    response_model=SaidaOut,
    status_code=status.HTTP_201_CREATED,
)
def registrar_saida(
    payload: SaidaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Registra uma nova saída. Requer autenticação JWT.
    """
    obj = Saida(
        base=payload.base,
        entregador=payload.entregador,
        estacao=payload.estacao,
        codigo=payload.codigo,
        servico=payload.servico or "padrao",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


# Execução local (opcional)
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main_updated:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "true").lower() == "true",
    )
