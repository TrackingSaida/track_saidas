from __future__ import annotations

from sqlalchemy import (
    Column, BigInteger, Integer, Text, Numeric, Date, DateTime, Boolean,
    text, UniqueConstraint
)
from sqlalchemy.sql import func

from db import Base

# ==========================
# Tabela: users
# ==========================
class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)
    email = Column(Text, nullable=False)
    password_hash = Column(Text, nullable=False)   # <- senha única do usuário (login)
    username = Column(Text, nullable=False)
    contato = Column(Text, nullable=False)

    nome = Column(Text, nullable=True)
    sobrenome = Column(Text, nullable=True)

    status = Column(Boolean, nullable=False, server_default=text("true"))
    sub_base = Column(Text, nullable=True)

    # campos de “coletador”
    coletador = Column(Boolean, nullable=False, server_default=text("false"))
    username_entregador = Column(Text, nullable=True)
    tipo_de_cadastro = Column(Integer, nullable=True)  # definido como 3 pelo fluxo de entregadores

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} email={self.email!r} username={self.username!r} "
            f"coletador={self.coletador}>"
        )


# ==========================
# Tabela: entregador
# ==========================
class Entregador(Base):
    __tablename__ = "entregador"
    __table_args__ = (
        UniqueConstraint("sub_base", "username_entregador", name="uq_entregador_subbase_username"),
    )

    id_entregador = Column(BigInteger, primary_key=True)
    nome = Column(Text, nullable=False)
    telefone = Column(Text, nullable=False)
    ativo = Column(Boolean, nullable=False, server_default=text("false"))
    documento = Column(Text, nullable=True)
    data_cadastro = Column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    sub_base = Column(Text, nullable=True)

    # endereço
    rua = Column(Text, nullable=False)
    numero = Column(Text, nullable=False)
    complemento = Column(Text, nullable=False)
    cep = Column(Text, nullable=False)
    cidade = Column(Text, nullable=False)
    bairro = Column(Text, nullable=False)

    # “perfil de coletador” (sem senha aqui!)
    coletador = Column(Boolean, nullable=False, server_default=text("false"))
    username_entregador = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Entregador id_entregador={self.id_entregador} nome={self.nome!r}>"
