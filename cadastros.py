# routers/cadastros.py
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Column, BigInteger, Text, Boolean, Date, DateTime, func, select, update, delete
from sqlalchemy.orm import Session, declarative_base

# Reaproveita sessão e Base do main.py (não cria outro engine)
from main import Base, get_db

router = APIRouter(tags=["Cadastros"])

# ------------------------------------------------------------------------------
# ORM MODELS (mapeiam tabelas EXISTENTES no Postgres)
#  - Não chamamos create_all aqui (seguindo seu padrão)
#  - Os nomes de coluna precisam bater com o banco.
#  - Se você tiver uma coluna literalmente chamada R$ em "users",
#    mapeamos assim: Column('R$', Text, key='valor_rs')
# ------------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"
    id           = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    email        = Column(Text, nullable=False)
    senha        = Column(Text, nullable=True)
    username     = Column(Text, nullable=True)
    contato      = Column(Text, nullable=True)
    status       = Column(Text, nullable=True)        # ex.: ativo/inativo
    cobranca     = Column(Text, nullable=True)        # ex.: valor/mensal
    # se a tabela tiver uma coluna com nome estranho "R$", mapeie assim:
    valor_rs     = Column('R$', Text, nullable=True, key='valor_rs')  # opcional; remova se não existir
    mensalidade  = Column(Date, nullable=True)        # data
    creditos     = Column(Text, nullable=True)        # pode ser money/text, mapeado como text por segurança

class Entregador(Base):
    __tablename__ = "entregador"
    id         = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    email_base = Column(Text, nullable=True)
    nome       = Column(Text, nullable=False)
    telefone   = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())

class Estacao(Base):
    __tablename__ = "estacao"
    id         = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    email_base = Column(Text, nullable=True)
    estacao    = Column(Text, nullable=False)

# ------------------------------------------------------------------------------
# Pydantic Schemas (entrada/saída)
# ------------------------------------------------------------------------------

# USERS
class UserIn(BaseModel):
    email: str = Field(min_length=3)
    senha: Optional[str] = None
    username: Optional[str] = None
    contato: Optional[str] = None
    status: Optional[str] = None
    cobranca: Optional[str] = None
    valor_rs: Optional[str] = None     # remova se não existir a coluna "R$"
    mensalidade: Optional[Date] = None
    creditos: Optional[str] = None

class UserOut(UserIn):
    id: int

# ENTREGADORES
class EntregadorIn(BaseModel):
    email_base: Optional[str] = None
    nome: str = Field(min_length=1)
    telefone: Optional[str] = None

class EntregadorOut(EntregadorIn):
    id: int

# ESTACOES
class EstacaoIn(BaseModel):
    email_base: Optional[str] = None
    estacao: str = Field(min_length=1)

class EstacaoOut(EstacaoIn):
    id: int

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def asdict(obj) -> Dict[str, Any]:
    """Converte ORM → dict usando os schemas de saída."""
    d = obj.__dict__.copy()
    d.pop("_sa_instance_state", None)
    return d

# ------------------------------------------------------------------------------
# USERS endpoints
# ------------------------------------------------------------------------------

@router.get("/cadastros/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), limit: int = 100):
    rows = db.execute(select(User).limit(limit)).scalars().all()
    return [UserOut(**asdict(r)) for r in rows]

@router.post("/cadastros/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserIn, db: Session = Depends(get_db)):
    obj = User(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return UserOut(**asdict(obj))

@router.put("/cadastros/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserIn, db: Session = Depends(get_db)):
    obj = db.get(User, user_id)
    if not obj:
        raise HTTPException(404, "User não encontrado")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return UserOut(**asdict(obj))

@router.delete("/cadastros/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, db: Session = Depends(get_db)):
    obj = db.get(User, user_id)
    if not obj:
        raise HTTPException(404, "User não encontrado")
    db.delete(obj)
    db.commit()
    return

# ------------------------------------------------------------------------------
# ENTREGADORES endpoints
# ------------------------------------------------------------------------------

@router.get("/cadastros/entregadores", response_model=List[EntregadorOut])
def list_entregadores(db: Session = Depends(get_db), limit: int = 200):
    rows = db.execute(select(Entregador).limit(limit)).scalars().all()
    return [EntregadorOut(**asdict(r)) for r in rows]

@router.post("/cadastros/entregadores", response_model=EntregadorOut, status_code=status.HTTP_201_CREATED)
def create_entregador(payload: EntregadorIn, db: Session = Depends(get_db)):
    obj = Entregador(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return EntregadorOut(**asdict(obj))

@router.put("/cadastros/entregadores/{entregador_id}", response_model=EntregadorOut)
def update_entregador(entregador_id: int, payload: EntregadorIn, db: Session = Depends(get_db)):
    obj = db.get(Entregador, entregador_id)
    if not obj:
        raise HTTPException(404, "Entregador não encontrado")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return EntregadorOut(**asdict(obj))

@router.delete("/cadastros/entregadores/{entregador_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entregador(entregador_id: int, db: Session = Depends(get_db)):
    obj = db.get(Entregador, entregador_id)
    if not obj:
        raise HTTPException(404, "Entregador não encontrado")
    db.delete(obj)
    db.commit()
    return

# ------------------------------------------------------------------------------
# ESTACOES endpoints
# ------------------------------------------------------------------------------

@router.get("/cadastros/estacoes", response_model=List[EstacaoOut])
def list_estacoes(db: Session = Depends(get_db), limit: int = 200):
    rows = db.execute(select(Estacao).limit(limit)).scalars().all()
    return [EstacaoOut(**asdict(r)) for r in rows]

@router.post("/cadastros/estacoes", response_model=EstacaoOut, status_code=status.HTTP_201_CREATED)
def create_estacao(payload: EstacaoIn, db: Session = Depends(get_db)):
    obj = Estacao(**payload.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return EstacaoOut(**asdict(obj))

@router.put("/cadastros/estacoes/{estacao_id}", response_model=EstacaoOut)
def update_estacao(estacao_id: int, payload: EstacaoIn, db: Session = Depends(get_db)):
    obj = db.get(Estacao, estacao_id)
    if not obj:
        raise HTTPException(404, "Estação não encontrada")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(obj, k, v)
    db.commit()
    db.refresh(obj)
    return EstacaoOut(**asdict(obj))

@router.delete("/cadastros/estacoes/{estacao_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_estacao(estacao_id: int, db: Session = Depends(get_db)):
    obj = db.get(Estacao, estacao_id)
    if not obj:
        raise HTTPException(404, "Estação não encontrada")
    db.delete(obj)
    db.commit()
    return
