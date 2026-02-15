-- ============================================================
-- Migração: Fechamento de Bases (Gerar Cobrança de Coletas)
-- Tabelas: base_fechamentos, base_fechamento_itens
-- ============================================================

-- Tabela base_fechamentos (cabeçalho)
CREATE TABLE IF NOT EXISTS base_fechamentos (
    id_fechamento BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    base TEXT NOT NULL,
    periodo_inicio DATE NOT NULL,
    periodo_fim DATE NOT NULL,
    valor_bruto NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    valor_cancelados NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    valor_final NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    status TEXT NOT NULL DEFAULT 'GERADO',
    criado_em TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_base_fechamento_periodo UNIQUE (sub_base, base, periodo_inicio, periodo_fim)
);

-- Tabela base_fechamento_itens (detalhes por dia)
CREATE TABLE IF NOT EXISTS base_fechamento_itens (
    id_item BIGSERIAL PRIMARY KEY,
    id_fechamento BIGINT NOT NULL REFERENCES base_fechamentos(id_fechamento) ON DELETE CASCADE,
    data DATE NOT NULL,
    shopee INTEGER NOT NULL DEFAULT 0,
    mercado_livre INTEGER NOT NULL DEFAULT 0,
    avulso INTEGER NOT NULL DEFAULT 0,
    cancelados_shopee INTEGER NOT NULL DEFAULT 0,
    cancelados_ml INTEGER NOT NULL DEFAULT 0,
    cancelados_avulso INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_base_fechamento_item_data UNIQUE (id_fechamento, data)
);

CREATE INDEX IF NOT EXISTS ix_base_fechamentos_sub_base ON base_fechamentos(sub_base);
CREATE INDEX IF NOT EXISTS ix_base_fechamentos_base ON base_fechamentos(base);
CREATE INDEX IF NOT EXISTS ix_base_fechamentos_status ON base_fechamentos(status);
CREATE INDEX IF NOT EXISTS ix_base_fechamento_itens_id_fechamento ON base_fechamento_itens(id_fechamento);
