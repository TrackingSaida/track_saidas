from __future__ import annotations

from sqlalchemy import Column, Integer, Text, Numeric, Date
from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(Text, nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    username = Column(Text, nullable=False, unique=True)
    contato = Column(Text, nullable=False)

    status = Column(Text, nullable=True, default="ativo")
    cobranca = Column(Text, nullable=True)
    valor = Column(Numeric(12, 2), nullable=True)
    mensalidade = Column(Date, nullable=True)
    creditos = Column(Numeric(12, 2), nullable=True, default=0.00)
    base = Column(Text, nullable=True)   # 👈 ESSENCIAL PARA O /entregadores
