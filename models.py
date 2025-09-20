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
# Tabela: coletas
# ==========================
class Coleta(Base):
    __tablename__ = "coletas"

    id_coleta = Column(BigInteger, primary_key=True, autoincrement=True)

    # mesmo nome da coluna na planilha, mas em snake_case
    timestamp = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    sub_base  = Column(Text, nullable=True)
    base      = Column(Text, nullable=True)
    username_entregador = Column(Text, nullable=True)

    # contadores por tipo (vêm como inteiros)
    shopee         = Column(Integer, nullable=False, server_default=text("0"))
    mercado_livre  = Column(Integer, nullable=False, server_default=text("0"))
    avulso         = Column(Integer, nullable=False, server_default=text("0"))
    nfe            = Column(Integer, nullable=False, server_default=text("0"))

    # total monetário
    valor_total = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))

    def __repr__(self) -> str:
        return f"<Coleta id_coleta={self.id_coleta} sub_base={self.sub_base!r} username_entregador={self.username_entregador!r}>"

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
