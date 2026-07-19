-- Migração: critério de fechamento por sub_base + snapshot de itens
-- Executar manualmente no banco antes de subir a nova versão.
--
-- Exemplo:
--   psql "CONNECTION_STRING" -f migrations/fechamento_criterio_subbase.sql

-- 1. Configuração do critério por sub_base
CREATE TABLE IF NOT EXISTS sub_base_fechamento_config (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    modo TEXT NOT NULL DEFAULT 'operacional',
    updated_by BIGINT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_sub_base_fechamento_config_sub_base UNIQUE (sub_base),
    CONSTRAINT ck_sub_base_fechamento_config_modo
        CHECK (modo IN ('operacional', 'confirmacao_entrega'))
);

CREATE INDEX IF NOT EXISTS idx_sub_base_fechamento_config_sub_base
    ON sub_base_fechamento_config (sub_base);

-- 2. Critério aplicado no snapshot do fechamento
ALTER TABLE entregador_fechamentos
ADD COLUMN IF NOT EXISTS modo_criterio TEXT NOT NULL DEFAULT 'operacional';

ALTER TABLE entregador_fechamentos
DROP CONSTRAINT IF EXISTS ck_entregador_fechamentos_modo_criterio;

ALTER TABLE entregador_fechamentos
ADD CONSTRAINT ck_entregador_fechamentos_modo_criterio
    CHECK (modo_criterio IN ('operacional', 'confirmacao_entrega'));

-- 3. Itens auditáveis do fechamento
CREATE TABLE IF NOT EXISTS entregador_fechamento_itens (
    id_item BIGSERIAL PRIMARY KEY,
    id_fechamento BIGINT NOT NULL
        REFERENCES entregador_fechamentos(id_fechamento) ON DELETE CASCADE,
    id_saida BIGINT NOT NULL,
    codigo TEXT NULL,
    id_motoboy BIGINT NULL,
    id_entregador BIGINT NULL,
    servico TEXT NULL,
    status_evento TEXT NOT NULL,
    valor NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    is_grande BOOLEAN NOT NULL DEFAULT false,
    data_operacional DATE NULL,
    data_confirmacao DATE NULL,
    id_historico_atribuicao BIGINT NULL,
    id_historico_confirmacao BIGINT NULL,
    criado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_entregador_fechamento_item_fechamento_saida
        UNIQUE (id_fechamento, id_saida),
    CONSTRAINT uq_entregador_fechamento_item_saida
        UNIQUE (id_saida)
);

CREATE INDEX IF NOT EXISTS idx_entregador_fechamento_itens_fechamento
    ON entregador_fechamento_itens (id_fechamento);

CREATE INDEX IF NOT EXISTS idx_entregador_fechamento_itens_motoboy
    ON entregador_fechamento_itens (id_motoboy);

CREATE INDEX IF NOT EXISTS idx_entregador_fechamento_itens_codigo
    ON entregador_fechamento_itens (codigo);
