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

    # credenciais/identificação
    email = Column(Text, nullable=False)            # e-mail do usuário
    password_hash = Column(Text, nullable=False)    # SENHA HASH (única senha usada no sistema)
    username = Column(Text, nullable=False)         # pode ser o mesmo do "username_entregador"
    contato = Column(Text, nullable=False)          # telefone/celular

    # dados adicionais
    nome = Column(Text, nullable=True)
    sobrenome = Column(Text, nullable=True)

    # status geral do usuário
    status = Column(Boolean, nullable=False, server_default=text("true"))

    # escopo/base
    sub_base = Column(Text, nullable=True)

    # PERFIL DE COLETADOR (NOVO)
    coletador = Column(Boolean, nullable=False, server_default=text("false"))
    username_entregador = Column(Text, nullable=True)  # espelha o username do entregador quando aplicável
    role = Column(Integer, nullable=False, server_default="2") 

    def __repr__(self) -> str:
        return (
            f"<User id={self.id} email={self.email!r} username={self.username!r} "
            f"coletador={self.coletador} sub_base={self.sub_base!r}>"
        )

# ==========================
# Tabela: owner
# ==========================

class Owner(Base):
    __tablename__ = "owner"

    id_owner = Column(BigInteger, primary_key=True)
    email = Column(Text, nullable=False, server_default=text("''::text"))
    username = Column(Text, nullable=False)
    valor = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    sub_base = Column(Text, nullable=True)
    contato = Column(Text, nullable=True)

    ativo = Column(Boolean, nullable=False, server_default=text("true"))
    ignorar_coleta = Column(Boolean, nullable=False, server_default=text("false"))


    def __repr__(self) -> str:
        return f"<Owner id_owner={self.id_owner} username={self.username!r} ativo={self.ativo}>"



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

    valor_total = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))

    def __repr__(self) -> str:
        return f"<Coleta id_coleta={self.id_coleta} sub_base={self.sub_base!r} username_entregador={self.username_entregador!r}>"


# ==========================
# Tabela: base (preços por base/sub_base)
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
    ativo = Column(Boolean, nullable=False, server_default=text("false"))

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
    base = Column(Text, nullable=True)              # NOVA COLUNA (de onde veio a mercadoria)
    username = Column(Text, nullable=True)
    entregador_id = Column(BigInteger, nullable=True)
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
    sub_base = Column(Text, nullable=True)          # base herdada do solicitante

    # dados do entregador
    nome = Column(Text, nullable=False)
    telefone = Column(Text, nullable=False)
    documento = Column(Text, nullable=True)

    ativo = Column(Boolean, nullable=False, server_default=text("false"))
    data_cadastro = Column(Date, nullable=False, server_default=text("CURRENT_DATE"))

    # endereço
    rua = Column(Text, nullable=False)
    numero = Column(Text, nullable=False)
    complemento = Column(Text, nullable=False)
    cep = Column(Text, nullable=False)
    cidade = Column(Text, nullable=False)
    bairro = Column(Text, nullable=False)

    # perfil coletador
    coletador = Column(Boolean, nullable=False, server_default=text("false"))
    username_entregador = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<Entregador id_entregador={self.id_entregador} nome={self.nome!r} coletador={self.coletador}>"

# ==========================
# Tabela: mercado_livre_tokens
# ==========================
class MercadoLivreToken(Base):
    __tablename__ = "mercado_livre_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id_ml = Column(BigInteger, nullable=False)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    expires_at = Column(DateTime(timezone=False), nullable=False)
    criado_em = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<MercadoLivreToken id={self.id} user_id_ml={self.user_id_ml}>"


# ==========================
# Tabela: owner_cobranca_itens
# ==========================
class OwnerCobrancaItem(Base):
    __tablename__ = "owner_cobranca_itens"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sub_base = Column(Text, nullable=False)
    id_coleta = Column(BigInteger, nullable=True)
    id_saida = Column(BigInteger, nullable=True)


    valor = Column(Numeric(12, 2), nullable=False)
    timestamp = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)

    periodo_inicio = Column(Date, nullable=True)
    periodo_fim = Column(Date, nullable=True)
    fechado = Column(Boolean, nullable=False, server_default=text("false"))


 # ==========================
 # Tabela: saidas_detail
 # ==========================       
class SaidaDetail(Base):
    __tablename__ = "saidas_detail"

    id_detail = Column(BigInteger, primary_key=True, autoincrement=True)

    id_saida = Column(BigInteger, nullable=False)

    id_entregador = Column(BigInteger, nullable=False)
 # entregador responsável pela entrega

    status = Column(Text, nullable=False, server_default=text("'Em Rota'"))
    tentativa = Column(Integer, nullable=False, server_default=text("1"))

    motivo_ocorrencia = Column(Text, nullable=True)
    observacao_ocorrencia = Column(Text, nullable=True)

    tipo_recebedor = Column(Text, nullable=True)
    nome_recebedor = Column(Text, nullable=True)
    tipo_documento = Column(Text, nullable=True)
    numero_documento = Column(Text, nullable=True)
    observacao_entrega = Column(Text, nullable=True)

    foto_url = Column(Text, nullable=True)

    dest_nome = Column(Text, nullable=True)
    dest_rua = Column(Text, nullable=True)
    dest_numero = Column(Text, nullable=True)
    dest_complemento = Column(Text, nullable=True)
    dest_bairro = Column(Text, nullable=True)
    dest_cidade = Column(Text, nullable=True)
    dest_estado = Column(Text, nullable=True)
    dest_cep = Column(Text, nullable=True)
    dest_contato = Column(Text, nullable=True)

    timestamp = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<SaidaDetail id_detail={self.id_detail} id_saida={self.id_saida}>"