from __future__ import annotations

from sqlalchemy import (
    Column,
    BigInteger,
    Integer,
    Text,
    Numeric,
    Date,
    DateTime,
    Boolean,
    text,
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
    password_hash = Column(Text, nullable=False)
    username = Column(Text, nullable=False)
    contato = Column(Text, nullable=False)

    nome = Column(Text, nullable=True)
    sobrenome = Column(Text, nullable=True)

    status = Column(Boolean, nullable=False, server_default=text("true"))
    sub_base = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} email={self.email!r} username={self.username!r} "
            f"nome={self.nome!r} sobrenome={self.sobrenome!r} status={self.status}>"
        )


# ==========================
# Tabela: owner
# (campos de cobrança/planos)
# ==========================
class Owner(Base):
    __tablename__ = "owner"

    id_owner = Column(BigInteger, primary_key=True)
    email = Column(Text, nullable=False, server_default=text("''::text"))
    username = Column(Text, nullable=False)
    cobranca = Column(Text, nullable=True)
    valor = Column(Numeric(12, 2), nullable=True)
    mensalidade = Column(Date, nullable=True)
    creditos = Column(Numeric(12, 2), nullable=True, server_default=text("0.00"))
    sub_base = Column(Text, nullable=True)
    contato = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Owner id_owner={self.id_owner} username={self.username!r}>"


# ==========================
# Tabela: servico_padroes
# ==========================
class ServicoPadrao(Base):
    __tablename__ = "servico_padroes"

    id_servico = Column(BigInteger, primary_key=True, autoincrement=True)
    servico = Column(Text, nullable=False)
    regex = Column(Text, nullable=False)
    normalizar = Column(Boolean, nullable=False, server_default=text("false"))
    prioridade = Column(Integer, nullable=False, server_default=text("100"))
    ativo = Column(Boolean, nullable=False, server_default=text("true"))
    criado_em = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    atualizado_em = Column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ServicoPadrao id_servico={self.id_servico} servico={self.servico!r}>"


# ==========================
# Tabela: saidas
# ==========================
class Saida(Base):
    __tablename__ = "saidas"

    id_saida = Column(BigInteger, primary_key=True)
    timestamp = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    data = Column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    sub_base = Column(Text, nullable=True)
    username = Column(Text, nullable=True)
    entregador = Column(Text, nullable=True)
    codigo = Column(Text, nullable=True)
    servico = Column(Text, nullable=True, server_default=text("'padrao'::text"))
    status = Column(Text, nullable=True, server_default=text("'saiu'::text"))

    def __repr__(self) -> str:
        return f"<Saida id_saida={self.id_saida} data={self.data} servico={self.servico!r}>"


# ==========================
# Tabela: entregador
# ==========================
class Entregador(Base):
    __tablename__ = "entregador"

    id_entregador = Column(BigInteger, primary_key=True)
    nome = Column(Text, nullable=False)
    telefone = Column(Text, nullable=False)
    ativo = Column(Boolean, nullable=False, server_default=text("false"))
    documento = Column(Text, nullable=True)
    data_cadastro = Column(Date, nullable=False, server_default=text("CURRENT_DATE"))
    sub_base = Column(Text, nullable=True)


    def __repr__(self) -> str:
        return f"<Entregador id_entregador={self.id_entregador} nome={self.nome!r}>"
