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
    ForeignKey,
)
from sqlalchemy.sql import func

from db import Base
from sqlalchemy import event
from sqlalchemy.orm import Session, relationship


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
    sub_base = Column(Text, nullable=True)
    base = Column(Text, nullable=True)
    username_entregador = Column(Text, nullable=True)

    shopee = Column(Integer, nullable=False, server_default=text("0"))
    mercado_livre = Column(Integer, nullable=False, server_default=text("0"))
    avulso = Column(Integer, nullable=False, server_default=text("0"))

    valor_total = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))

    saidas = relationship("Saida", back_populates="coleta")

    def __repr__(self) -> str:
        return (
            f"<Coleta id_coleta={self.id_coleta} "
            f"sub_base={self.sub_base!r} username_entregador={self.username_entregador!r}>"
        )


# ==========================
# Tabela: base (preços por base/sub_base)
# ==========================
class BasePreco(Base):
    __tablename__ = "base"

    id_base = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    base = Column(Text, nullable=True)
    sub_base = Column(Text, nullable=True)
    username = Column(Text, nullable=True)

    shopee = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    ml = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
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
    base = Column(Text, nullable=True)  # de onde veio a mercadoria
    username = Column(Text, nullable=True)
    entregador_id = Column(BigInteger, nullable=True)
    entregador = Column(Text, nullable=True)

    codigo = Column(Text, nullable=True)
    servico = Column(Text, nullable=True, server_default=text("'padrao'::text"))
    status = Column(Text, nullable=True, server_default=text("'saiu'::text"))
    id_coleta = Column(BigInteger, ForeignKey("coletas.id_coleta"), nullable=True)

    coleta = relationship("Coleta", back_populates="saidas")

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
    sub_base = Column(Text, nullable=True)  # base herdada do solicitante

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

    preco = relationship("EntregadorPreco", uselist=False, back_populates="entregador")

    def __repr__(self) -> str:
        return f"<Entregador id_entregador={self.id_entregador} nome={self.nome!r} coletador={self.coletador}>"


# ==========================
# Tabela: entregador_preco_global
# ==========================
class EntregadorPrecoGlobal(Base):
    __tablename__ = "entregador_preco_global"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sub_base = Column(Text, nullable=False, unique=True)

    shopee_valor = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    ml_valor = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    avulso_valor = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<EntregadorPrecoGlobal id={self.id} sub_base={self.sub_base!r}>"


# ==========================
# Tabela: entregador_preco
# ==========================
class EntregadorPreco(Base):
    __tablename__ = "entregador_preco"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    id_entregador = Column(
        BigInteger,
        ForeignKey("entregador.id_entregador"),
        nullable=False,
        unique=True,
    )

    shopee_valor = Column(Numeric(12, 2), nullable=True)
    ml_valor = Column(Numeric(12, 2), nullable=True)
    avulso_valor = Column(Numeric(12, 2), nullable=True)

    usa_preco_global = Column(Boolean, nullable=False, server_default=text("true"))

    created_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    entregador = relationship("Entregador", back_populates="preco")

    def __repr__(self) -> str:
        return f"<EntregadorPreco id={self.id} id_entregador={self.id_entregador}>"


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
# Tabela: shopee_tokens
# ==========================
class ShopeeToken(Base):
    __tablename__ = "shopee_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shop_id = Column(BigInteger, nullable=False)
    main_account_id = Column(BigInteger, nullable=True)

    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=False), nullable=True)
    criado_em = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ShopeeToken id={self.id} shop_id={self.shop_id}>"


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
# Tabela: entregador_fechamentos
# ==========================
class EntregadorFechamento(Base):
    __tablename__ = "entregador_fechamentos"
    __table_args__ = (
        UniqueConstraint(
            "sub_base", "id_entregador", "periodo_inicio", "periodo_fim",
            name="uq_entregador_fechamento_periodo",
        ),
    )

    id_fechamento = Column(BigInteger, primary_key=True, autoincrement=True)
    sub_base = Column(Text, nullable=False)
    id_entregador = Column(BigInteger, nullable=False)
    username_entregador = Column(Text, nullable=False)

    periodo_inicio = Column(Date, nullable=False)
    periodo_fim = Column(Date, nullable=False)

    valor_base = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    valor_adicao = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    motivo_adicao = Column(Text, nullable=True)
    valor_subtracao = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    motivo_subtracao = Column(Text, nullable=True)

    valor_final = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    status = Column(Text, nullable=False, server_default=text("'fechado'::text"))

    criado_em = Column(DateTime(timezone=False), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"<EntregadorFechamento id_fechamento={self.id_fechamento} "
            f"id_entregador={self.id_entregador} status={self.status!r}>"
        )


# ==========================
# Tabela: saidas_detail
# ==========================
class SaidaDetail(Base):
    __tablename__ = "saidas_detail"

    id_detail = Column(BigInteger, primary_key=True, autoincrement=True)

    id_saida = Column(BigInteger, nullable=False)
    id_entregador = Column(BigInteger, nullable=False)  # entregador responsável pela entrega

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


@event.listens_for(Saida, "after_update")
def saida_after_update(mapper, connection, target: Saida):
    """
    Recalcula automaticamente a coleta sempre que uma saída for alterada.
    """
    if not target.id_coleta:
        return

    # Cria sessão vinculada a essa conexão
    db = Session(bind=connection)

    try:
        # Import atrasado para evitar circular import
        from coletas import recalcular_coleta

        recalcular_coleta(db, target.id_coleta)
        db.commit()
    except Exception:
        db.rollback()
        raise

# ==========================
# Tabela: logs_leitura
# ==========================
class LogLeitura(Base):
    __tablename__ = "logs_leitura"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # escopo / auditoria
    sub_base = Column(Text, nullable=False, index=True)
    username = Column(Text, nullable=False, index=True)

    # contexto da leitura
    origem = Column(Text, nullable=False)   # camera | teclado
    tipo = Column(Text, nullable=False)     # saida | coleta

    codigo = Column(Text, nullable=True, index=True)

    # resultado final
    resultado = Column(Text, nullable=False)

    # métricas antigas (mantidas)
    delta_from_last_read_ms = Column(Numeric(12, 3), nullable=True)
    delta_read_to_send_ms = Column(Numeric(12, 3), nullable=True)
    delta_send_to_response_ms = Column(Numeric(12, 3), nullable=True)

    # timestamps
    ts_read = Column(Numeric(16, 6), nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=False, server_default=func.now())

    # ─────────────────────────────
    # 🔥 NOVAS MÉTRICAS (FRONT)
    front_processing_ms = Column(Numeric(12, 3), nullable=True)
    front_network_ms = Column(Numeric(12, 3), nullable=True)
    front_total_ms = Column(Numeric(12, 3), nullable=True)

    # 🔥 NOVA MÉTRICA (BACK)
    backend_processing_ms = Column(Numeric(12, 3), nullable=True)

    # correlação / controle
    request_id = Column(Text, nullable=True)
    attempt = Column(Integer, nullable=True)

    # contexto do device
    network_status = Column(Text, nullable=True)
    device_type = Column(Text, nullable=True)
    os = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<LogLeitura id={self.id} tipo={self.tipo} "
            f"origem={self.origem} resultado={self.resultado}>"
        )
