import os
from datetime import datetime, date
from typing import Optional

from fastapi import FastAPI, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from sqlalchemy import create_engine, Column, BigInteger, Text, Date, DateTime, func
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
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")  # ajuste no .env/Render
API_PREFIX   = os.getenv("API_PREFIX", "/api")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ------------------------------------------------------------------------------
# MODELO: tabela `saidas`
# ------------------------------------------------------------------------------
class Saida(Base):
    __tablename__ = "saidas"

    id_saida   = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    timestamp  = Column(DateTime(timezone=False), server_default=func.now())
    data       = Column(Date, server_default=func.current_date())

    base       = Column(Text, nullable=True)
    entregador = Column(Text, nullable=True)
    codigo     = Column(Text, nullable=True)
    servico    = Column(Text, nullable=True)
    status     = Column(Text, nullable=True)
    estacao    = Column(Text, nullable=True)

# ------------------------------------------------------------------------------
# SCHEMAS
# ------------------------------------------------------------------------------
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

    base: Optional[str]
    entregador: Optional[str]
    codigo: Optional[str]
    servico: Optional[str]
    status: Optional[str]
    estacao: Optional[str]

    class Config:
        from_attributes = True

# ------------------------------------------------------------------------------
# APP
# ------------------------------------------------------------------------------
app = FastAPI(title="API Saídas", version="0.2.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Sessão do banco (exposta para os módulos de rota)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ------------------------------------------------------------------------------
# ROTAS EXTERNAS (cadastros)
# ------------------------------------------------------------------------------
# Importa os três módulos de rota separados
from users_routes import router as users_router
from entregador_routes import router as entregadores_router
from estacao_routes import router as estacoes_router

# Registra com prefixo comum (ex.: /api/users, /api/entregadores, /api/estacoes)
app.include_router(users_router,        prefix=API_PREFIX)
app.include_router(entregadores_router, prefix=API_PREFIX)
app.include_router(estacoes_router,     prefix=API_PREFIX)

# Healthcheck
@app.get(f"{API_PREFIX}/health")
def health():
    return {"status": "ok"}

# ------------------------------------------------------------------------------
# Endpoint: registrar saída
# ------------------------------------------------------------------------------
@app.post(f"{API_PREFIX}/saidas/registrar", response_model=SaidaOut, status_code=status.HTTP_201_CREATED)
def registrar_saida(payload: SaidaCreate, db: Session = Depends(get_db)):
    obj = Saida(
        base=payload.base,
        entregador=payload.entregador,
        estacao=payload.estacao,
        codigo=payload.codigo,
        servico=payload.servico or "padrao",
        # status="saiu",
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj

# OBS: Não chamamos Base.metadata.create_all(bind=engine) aqui para não
# criar/alterar tabelas no PostgreSQL sem migração. Use apenas em dev/local.
# Base.metadata.create_all(bind=engine)
