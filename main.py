import os
from datetime import datetime, date
from typing import Optional

from fastapi import FastAPI, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from sqlalchemy import create_engine, Column, BigInteger, Text, Boolean, Date, DateTime, func
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# --- .env (opcional) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")  # ajuste no .env ou variável do Render
API_PREFIX = os.getenv("API_PREFIX", "/api")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ------------------------------------------------------------------------------
# MODELO (mapeando sua tabela existente)
# ------------------------------------------------------------------------------
class Saida(Base):
    __tablename__ = "saidas"  # <<< se seu nome for outro, ajuste aqui

    id_saida   = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    timestamp  = Column(DateTime(timezone=False), server_default=func.now())
    data       = Column(Date, server_default=func.current_date())
    entregador = Column(Text, nullable=True)
    codigo     = Column(Text, nullable=True)
    servico    = Column(Text, nullable=True)
    status     = Column(Text, nullable=True)
    duplicado  = Column(Boolean, nullable=True)
    estacao    = Column(Text, nullable=True)


# ------------------------------------------------------------------------------
# SCHEMAS
# ------------------------------------------------------------------------------
class SaidaCreate(BaseModel):
    entregador: str = Field(min_length=1)
    estacao: str = Field(min_length=1)
    codigo: str = Field(min_length=1)
    servico: Optional[str] = None  # opcional

class SaidaOut(BaseModel):
    id_saida: int
    timestamp: datetime
    data: date
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    duplicado: Optional[bool]
    estacao: Optional[str]

    class Config:
        from_attributes = True


# ------------------------------------------------------------------------------
# APP
# ------------------------------------------------------------------------------
app = FastAPI(title="API Saídas", version="0.1.0")

# CORS (depois podemos travar pro seu domínio)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependência da sessão
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get(f"{API_PREFIX}/health")
def health():
    return {"status": "ok"}


# Endpoint ÚNICO: registrar
@app.post(f"{API_PREFIX}/saidas/registrar", response_model=SaidaOut, status_code=status.HTTP_201_CREATED)
def registrar_saida(payload: SaidaCreate, db: Session = Depends(get_db)):
    obj = Saida(
        entregador=payload.entregador,
        estacao=payload.estacao,
        codigo=payload.codigo,
        servico=payload.servico or "padrao",  # <- default se não vier
        # status e duplicado ficam nulos; timestamp e data vêm do servidor
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


# OBS: Não chamo Base.metadata.create_all() para não criar tabela errada no seu PostgreSQL.
# Se quiser usar SQLite local pra teste rápido, descomente a linha abaixo:
# Base.metadata.create_all(bind=engine)
