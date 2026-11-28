-- PRD-020: idempotência de POST /coletas/lote via X-Client-Action-Id.
-- Aditiva. Aplicar antes do deploy que lê a tabela no lote.

BEGIN;

CREATE TABLE IF NOT EXISTS coleta_client_actions (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    client_action_id TEXT NOT NULL,
    base TEXT NULL,
    codigo TEXT NULL,
    id_saida BIGINT NULL,
    id_coleta BIGINT NULL,
    id_execucao BIGINT NULL,
    resultado_json TEXT NOT NULL,
    criada_em TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_coleta_client_actions_sub_action UNIQUE (sub_base, client_action_id)
);

CREATE INDEX IF NOT EXISTS ix_coleta_client_actions_criada_em
    ON coleta_client_actions (criada_em);

COMMIT;
