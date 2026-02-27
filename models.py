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

    motoboy = relationship(
        "Motoboy",
        uselist=False,
        back_populates="user",
        cascade="all, delete-orphan",
    )

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
    modo_operacao = Column(Text, nullable=True, server_default=text("'codigo'"))
    # Flag para owners de teste (não considerados em dashboards/admin)
    teste = Column(Boolean, nullable=False, server_default=text("false"))

    def __repr__(self) -> str:
        return f"<Owner id_owner={self.id_owner} username={self.username!r} ativo={self.ativo}>"


# ==========================
# Tabela: motoboys
# ==========================
class Motoboy(Base):
    __tablename__ = "motoboys"

    id_motoboy = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    sub_base = Column(Text)
    documento = Column(Text)

    rua = Column(Text, nullable=False)
    numero = Column(Text, nullable=False)
    complemento = Column(Text)
    bairro = Column(Text, nullable=False)
    cidade = Column(Text, nullable=False)
    estado = Column(Text)
    cep = Column(Text, nullable=False)

    ativo = Column(Boolean, default=True)
    data_cadastro = Column(Date)

    pode_ler_coleta = Column(Boolean, default=False, nullable=False)
    pode_ler_saida = Column(Boolean, default=True, nullable=False)

    user = relationship("User", back_populates="motoboy")
    sub_bases = relationship("MotoboySubBase", back_populates="motoboy", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Motoboy id_motoboy={self.id_motoboy} user_id={self.user_id}>"


# ==========================
# Tabela: motoboy_sub_base
# ==========================
class MotoboySubBase(Base):
    __tablename__ = "motoboy_sub_base"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    motoboy_id = Column(BigInteger, ForeignKey("motoboys.id_motoboy", ondelete="CASCADE"), nullable=False)
    sub_base = Column(Text, nullable=False)
    ativo = Column(Boolean, default=True)

    motoboy = relationship("Motoboy", back_populates="sub_bases")

    def __repr__(self) -> str:
        return f"<MotoboySubBase id={self.id} motoboy_id={self.motoboy_id} sub_base={self.sub_base!r}>"


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
    origem = Column(Text, nullable=False, server_default=text("'codigo'"))

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
    motoboy_id = Column(BigInteger, ForeignKey("motoboys.id_motoboy", ondelete="SET NULL"), nullable=True)
    # Deprecated: preferir evento "entregue" em saida_historico para data/hora; mantido em transição.
    data_hora_entrega = Column(DateTime(timezone=False), nullable=True)

    codigo = Column(Text, nullable=True)
    servico = Column(Text, nullable=True, server_default=text("'padrao'::text"))
    status = Column(Text, nullable=True, server_default=text("'saiu'::text"))
    id_coleta = Column(BigInteger, ForeignKey("coletas.id_coleta"), nullable=True)
    qr_payload_raw = Column(Text, nullable=True)  # Payload bruto do QR (ML) para gerar etiqueta reconhecível

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
    cancelado = Column(Boolean, nullable=False, server_default=text("false"))


# ==========================
# Tabela: entregador_fechamentos
# ==========================
class EntregadorFechamento(Base):
    __tablename__ = "entregador_fechamentos"
    __table_args__ = (
        # Unicidade por executor: entregador (id_entregador) ou motoboy (id_motoboy)
        UniqueConstraint(
            "sub_base", "id_entregador", "id_motoboy", "periodo_inicio", "periodo_fim",
            name="uq_entregador_fechamento_periodo",
        ),
    )

    id_fechamento = Column(BigInteger, primary_key=True, autoincrement=True)
    sub_base = Column(Text, nullable=False)
    id_entregador = Column(BigInteger, nullable=True)  # FK lógico para entregador.id_entregador
    id_motoboy = Column(BigInteger, ForeignKey("motoboys.id_motoboy", ondelete="CASCADE"), nullable=True)
    username_entregador = Column(Text, nullable=True)  # nome/username do entregador ou do user do motoboy

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
            f"id_entregador={self.id_entregador} id_motoboy={self.id_motoboy} status={self.status!r}>"
        )


# ==========================
# Tabela: base_fechamentos
# ==========================
class BaseFechamento(Base):
    __tablename__ = "base_fechamentos"
    __table_args__ = (
        UniqueConstraint(
            "sub_base", "base", "periodo_inicio", "periodo_fim",
            name="uq_base_fechamento_periodo",
        ),
    )

    id_fechamento = Column(BigInteger, primary_key=True, autoincrement=True)
    sub_base = Column(Text, nullable=False)
    base = Column(Text, nullable=False)

    periodo_inicio = Column(Date, nullable=False)
    periodo_fim = Column(Date, nullable=False)

    valor_bruto = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    valor_cancelados = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    valor_final = Column(Numeric(12, 2), nullable=False, server_default=text("0.00"))
    status = Column(Text, nullable=False, server_default=text("'GERADO'"))

    criado_em = Column(DateTime(timezone=False), server_default=func.now())

    itens = relationship("BaseFechamentoItem", back_populates="fechamento", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return (
            f"<BaseFechamento id_fechamento={self.id_fechamento} "
            f"base={self.base!r} status={self.status!r}>"
        )


# ==========================
# Tabela: base_fechamento_itens
# ==========================
class BaseFechamentoItem(Base):
    __tablename__ = "base_fechamento_itens"
    __table_args__ = (
        UniqueConstraint("id_fechamento", "data", name="uq_base_fechamento_item_data"),
    )

    id_item = Column(BigInteger, primary_key=True, autoincrement=True)
    id_fechamento = Column(BigInteger, ForeignKey("base_fechamentos.id_fechamento", ondelete="CASCADE"), nullable=False)

    data = Column(Date, nullable=False)

    shopee = Column(Integer, nullable=False, server_default=text("0"))
    mercado_livre = Column(Integer, nullable=False, server_default=text("0"))
    avulso = Column(Integer, nullable=False, server_default=text("0"))

    cancelados_shopee = Column(Integer, nullable=False, server_default=text("0"))
    cancelados_ml = Column(Integer, nullable=False, server_default=text("0"))
    cancelados_avulso = Column(Integer, nullable=False, server_default=text("0"))

    fechamento = relationship("BaseFechamento", back_populates="itens")

    def __repr__(self) -> str:
        return f"<BaseFechamentoItem id_item={self.id_item} data={self.data}>"


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

    latitude = Column(Numeric(12, 8), nullable=True)
    longitude = Column(Numeric(12, 8), nullable=True)
    endereco_formatado = Column(Text, nullable=True)
    endereco_origem = Column(Text, nullable=True)  # manual | ocr | voz

    timestamp = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<SaidaDetail id_detail={self.id_detail} id_saida={self.id_saida}>"


# ==========================
# Tabela: motivo_ausencia
# ==========================
class MotivoAusencia(Base):
    __tablename__ = "motivo_ausencia"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    descricao = Column(Text, nullable=False)
    ativo = Column(Boolean, nullable=False, server_default=text("true"))

    def __repr__(self) -> str:
        return f"<MotivoAusencia id={self.id} descricao={self.descricao!r}>"


# ==========================
# Tabela: saida_historico
# ==========================
class SaidaHistorico(Base):
    __tablename__ = "saida_historico"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    id_saida = Column(BigInteger, nullable=False, index=True)
    evento = Column(Text, nullable=False)
    status_anterior = Column(Text, nullable=True)
    status_novo = Column(Text, nullable=True)
    motoboy_id_anterior = Column(BigInteger, nullable=True)
    motoboy_id_novo = Column(BigInteger, nullable=True)
    user_id = Column(BigInteger, nullable=True)
    timestamp = Column(DateTime(timezone=False), nullable=False, server_default=func.now())
    payload = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<SaidaHistorico id={self.id} id_saida={self.id_saida} evento={self.evento!r}>"


# ==========================
# Tabela: rotas_motoboy
# ==========================
class RotasMotoboy(Base):
    __tablename__ = "rotas_motoboy"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    motoboy_id = Column(BigInteger, ForeignKey("motoboys.id_motoboy", ondelete="CASCADE"), nullable=False)
    data = Column(Date, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'ativa'"))  # preparando | ativa | finalizada | cancelada
    ordem_json = Column(Text, nullable=False)  # JSON array de id_saida
    parada_atual = Column(Integer, nullable=False, server_default=text("0"))
    iniciado_em = Column(DateTime(timezone=False), nullable=True)
    finalizado_em = Column(DateTime(timezone=False), nullable=True)

    def __repr__(self) -> str:
        return f"<RotasMotoboy id={self.id} motoboy_id={self.motoboy_id} status={self.status!r}>"


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

    # métrica backend (header X-Backend-Process-Time)
    backend_processing_ms = Column(Numeric(12, 3), nullable=True)

    # contexto do device
    network_status = Column(Text, nullable=True)
    device_type = Column(Text, nullable=True)
    os = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<LogLeitura id={self.id} tipo={self.tipo} "
            f"origem={self.origem} resultado={self.resultado}>"
        )
