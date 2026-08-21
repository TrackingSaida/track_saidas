-- Coleta flexível: agenda, execução diária, participantes e diária no fechamento.
-- Aplicar antes de subir a versão que usa os novos modelos.

BEGIN;

ALTER TABLE motoboys
    ADD COLUMN IF NOT EXISTS pode_realizar_coleta BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE motoboys
SET pode_realizar_coleta = TRUE
WHERE pode_ler_coleta IS TRUE;

-- O modo legado "saida" deixa de existir. A chave ignorar_coleta passa a ser
-- a única responsável por desligar completamente as coletas.
UPDATE owner
SET modo_operacao = 'codigo'
WHERE modo_operacao IS NULL
   OR modo_operacao NOT IN ('codigo', 'coleta_manual', 'ambos');

ALTER TABLE base
    ADD COLUMN IF NOT EXISTS dias_coleta JSON NOT NULL DEFAULT '[1,2,3,4,5,6]'::json,
    ADD COLUMN IF NOT EXISTS agenda_coleta_confirmada BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE entregador_preco_global
    ADD COLUMN IF NOT EXISTS coleta_valor NUMERIC(12,2) NOT NULL DEFAULT 0.00;

ALTER TABLE entregador_preco
    ADD COLUMN IF NOT EXISTS coleta_valor NUMERIC(12,2) NULL;

ALTER TABLE entregador_fechamentos
    ADD COLUMN IF NOT EXISTS valor_entregas NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    ADD COLUMN IF NOT EXISTS valor_coletas NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    ADD COLUMN IF NOT EXISTS qtd_dias_coleta INTEGER NOT NULL DEFAULT 0;

ALTER TABLE base_fechamentos
    ADD COLUMN IF NOT EXISTS recebido_em TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS recebido_por TEXT NULL;

-- Fechamentos antigos não possuíam diária de coleta.
UPDATE entregador_fechamentos
SET valor_entregas = valor_base
WHERE valor_entregas = 0 AND valor_coletas = 0 AND valor_base <> 0;

CREATE TABLE IF NOT EXISTS coleta_execucoes (
    id_execucao BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    base_id BIGINT NOT NULL REFERENCES base(id_base) ON DELETE RESTRICT,
    data_operacao DATE NOT NULL,
    modo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'coletado'
        CHECK (status IN ('coletado', 'sem_volume')),
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_coleta_execucao_base_dia
        UNIQUE (sub_base, base_id, data_operacao)
);

ALTER TABLE coleta_execucoes
    DROP CONSTRAINT IF EXISTS coleta_execucoes_modo_check;
ALTER TABLE coleta_execucoes
    DROP CONSTRAINT IF EXISTS ck_coleta_execucoes_modo;
ALTER TABLE coleta_execucoes
    ADD CONSTRAINT ck_coleta_execucoes_modo
    CHECK (modo IN ('codigo', 'coleta_manual', 'ambos'));

CREATE TABLE IF NOT EXISTS coleta_execucao_participantes (
    id_participante BIGSERIAL PRIMARY KEY,
    execucao_id BIGINT NOT NULL REFERENCES coleta_execucoes(id_execucao) ON DELETE CASCADE,
    sub_base TEXT NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    motoboy_id BIGINT NULL REFERENCES motoboys(id_motoboy) ON DELETE SET NULL,
    username TEXT NOT NULL,
    shopee INTEGER NOT NULL DEFAULT 0 CHECK (shopee >= 0),
    mercado_livre INTEGER NOT NULL DEFAULT 0 CHECK (mercado_livre >= 0),
    avulso INTEGER NOT NULL DEFAULT 0 CHECK (avulso >= 0),
    pacotes_g INTEGER NOT NULL DEFAULT 0 CHECK (pacotes_g >= 0),
    g_shopee INTEGER NOT NULL DEFAULT 0 CHECK (g_shopee >= 0),
    g_ml INTEGER NOT NULL DEFAULT 0 CHECK (g_ml >= 0),
    g_avulso INTEGER NOT NULL DEFAULT 0 CHECK (g_avulso >= 0),
    sem_volume BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'finalizado'
        CHECK (status IN ('em_coleta', 'finalizado')),
    client_request_id TEXT NULL,
    versao INTEGER NOT NULL DEFAULT 1,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_por_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT uq_coleta_participante_usuario UNIQUE (execucao_id, user_id),
    CONSTRAINT uq_coleta_participante_request UNIQUE (sub_base, client_request_id),
    CONSTRAINT ck_coleta_participante_volume CHECK (
        status = 'em_coleta' OR sem_volume OR shopee + mercado_livre + avulso > 0
    )
);

-- Compatibilidade para bancos onde a primeira versão da migration já foi aplicada.
ALTER TABLE coleta_execucoes
    DROP CONSTRAINT IF EXISTS coleta_execucoes_status_check;
ALTER TABLE coleta_execucoes
    DROP CONSTRAINT IF EXISTS ck_coleta_execucoes_status;
ALTER TABLE coleta_execucoes
    ADD CONSTRAINT ck_coleta_execucoes_status
    CHECK (status IN ('em_coleta', 'coletado', 'sem_volume'));

ALTER TABLE coleta_execucao_participantes
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'finalizado';
ALTER TABLE coleta_execucao_participantes
    DROP CONSTRAINT IF EXISTS coleta_execucao_participantes_status_check;
ALTER TABLE coleta_execucao_participantes
    DROP CONSTRAINT IF EXISTS ck_coleta_participantes_status;
ALTER TABLE coleta_execucao_participantes
    ADD CONSTRAINT ck_coleta_participantes_status
    CHECK (status IN ('em_coleta', 'finalizado'));
ALTER TABLE coleta_execucao_participantes
    DROP CONSTRAINT IF EXISTS ck_coleta_participante_volume;
ALTER TABLE coleta_execucao_participantes
    ADD CONSTRAINT ck_coleta_participante_volume
    CHECK (status = 'em_coleta' OR sem_volume OR shopee + mercado_livre + avulso > 0);

CREATE TABLE IF NOT EXISTS coleta_calendario_excecoes (
    id_excecao BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    base_id BIGINT NULL REFERENCES base(id_base) ON DELETE CASCADE,
    data DATE NOT NULL,
    tipo TEXT NOT NULL CHECK (
        tipo IN ('FERIADO', 'SEM_COLETA', 'COLETA_EXTRA', 'JUSTIFICADO')
    ),
    motivo TEXT NOT NULL,
    criado_por_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_coleta_calendario_subbase_dia
    ON coleta_calendario_excecoes (sub_base, data)
    WHERE base_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_coleta_calendario_base_dia
    ON coleta_calendario_excecoes (sub_base, base_id, data)
    WHERE base_id IS NOT NULL;

ALTER TABLE coletas
    ADD COLUMN IF NOT EXISTS execucao_id BIGINT NULL
        REFERENCES coleta_execucoes(id_execucao) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS participante_id BIGINT NULL
        REFERENCES coleta_execucao_participantes(id_participante) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_coleta_execucoes_subbase_data
    ON coleta_execucoes (sub_base, data_operacao);

CREATE INDEX IF NOT EXISTS ix_coleta_participantes_motoboy_data
    ON coleta_execucao_participantes (sub_base, motoboy_id, execucao_id);

CREATE INDEX IF NOT EXISTS ix_coleta_calendario_subbase_data
    ON coleta_calendario_excecoes (sub_base, data);

CREATE TABLE IF NOT EXISTS entregador_fechamento_coleta_itens (
    id_item BIGSERIAL PRIMARY KEY,
    id_fechamento BIGINT NOT NULL
        REFERENCES entregador_fechamentos(id_fechamento) ON DELETE CASCADE,
    sub_base TEXT NOT NULL,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE RESTRICT,
    data DATE NOT NULL,
    bases JSON NOT NULL DEFAULT '[]'::json,
    valor_diaria NUMERIC(12,2) NOT NULL,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_entregador_fechamento_coleta_dia UNIQUE (id_fechamento, data),
    CONSTRAINT uq_entregador_coleta_motoboy_dia UNIQUE (sub_base, motoboy_id, data)
);

CREATE INDEX IF NOT EXISTS ix_fechamento_coleta_motoboy_data
    ON entregador_fechamento_coleta_itens (sub_base, motoboy_id, data);

COMMIT;
