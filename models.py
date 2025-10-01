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
    UniqueConstraint,
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

    # perfil
    nome = Column(Text, nullable=True)
    sobrenome = Column(Text, nullable=True)

    status = Column(Boolean, nullable=False, server_default=text("true"))
    sub_base = Column(Text, nullable=True)

    # >>> novos/ajustados para coleta
    coletador = Column(Boolean, nullable=False, server_default=text("false"))
    username_entregador = Column(Text, nullable=True)
    senha_coletador = Column(Text, nullable=True)           # hash (bcrypt)
    tipo_de_cadastro = Column(Integer, nullable=True)       # será setado para 3

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} email={self.email!r} username={self.username!r} "
            f"nome={self.nome!r} status={self.status} coletador={self.coletador}>"
        )


# ==========================
# Tabela: owner
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

    timestamp = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    sub_base  = Column(Text, nullable=True)
    base      = Column(Text, nullable=True)
    username_entregador = Column(Text, nullable=True)

    shopee         = Column(Integer, nullable=False, server_default=text("0"))
    mercado_livre  = Column(Integer, nullable=False, server_default=text("0"))
    avulso         = Column(Integer, nullable=False, server_default=text("0"))
    nfe            = Column(Integer, nullable=False, server_default=text("0"))

    valor_total = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))

    def __repr__(self) -> str:
        return f"<Coleta id_coleta={self.id_coleta} sub_base={self.sub_base!r} username_entregador={self.username_entregador!r}>"


# ==========================
# Tabela: base (preços)
# ==========================
class BasePreco(Base):
    __tablename__ = "base"

    id_base   = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    base      = Column(Text, nullable=True)
    sub_base  = Column(Text, nullable=True)
    username  = Column(Text, nullable=True)

    shopee = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    ml     = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    avulso = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    nfe    = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))

    def __repr__(self) -> str:
        return f"<BasePreco id_base={self.id_base} sub_base={self.sub_base!r} username={self.username!r}>"


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

    # mantém APENAS estes dois aqui
    coletador = Column(Boolean, nullable=False, server_default=text("false"))
    username_entregador = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Entregador id_entregador={self.id_entregador} nome={self.nome!r}>"
