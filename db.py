# db.py
from __future__ import annotations

import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

def _normalize_db_url(url: str) -> str:
    """
    Render/Heroku às vezes expõem 'postgres://'.
    SQLAlchemy 2.x recomenda 'postgresql+psycopg2://'.
    Mantém query params (ex.: sslmode=require).
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido")

DATABASE_URL = _normalize_db_url(DATABASE_URL)

# Opcional: logar SQL em dev
ECHO_SQL = os.getenv("ECHO_SQL", "false").lower() in ("1", "true", "yes")

# Defaults dimensionados para bipagem concorrente no mobile.
# Ajuste no Render se o plano do Postgres tiver limite baixo de conexões.
# Fórmula aproximada por instância web: pool_size + max_overflow.
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 20)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 30)
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 30)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 300)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # evita conexões zumbis
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,  # mitiga SSL idle no Postgres gerenciado
    echo=ECHO_SQL,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# Base usada pelos models (models.py faz: from db import Base)
Base = declarative_base()

# Dependência do FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Opcional: criar tabelas manualmente se você não usar Alembic
def init_db():
    """
    Chame isso uma única vez na inicialização (ex.: no main.py)
    para criar as tabelas conforme os Models.
    """
    # Import adiado para evitar import circular
    from models import User, Owner, ServicoPadrao, Saida, Entregador, MercadoLivreToken, MLConexao  # noqa: F401
    Base.metadata.create_all(bind=engine)
