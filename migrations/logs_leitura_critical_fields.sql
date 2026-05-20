-- Campos mínimos para logs críticos de leitura.
-- Mantém tabela existente logs_leitura e coluna codigo existente.
-- Executar em produção com a mesma estratégia dos demais scripts de migration.

ALTER TABLE logs_leitura
    ADD COLUMN IF NOT EXISTS role INTEGER,
    ADD COLUMN IF NOT EXISTS motoboy_id BIGINT,
    ADD COLUMN IF NOT EXISTS id_saida BIGINT,
    ADD COLUMN IF NOT EXISTS origem_app TEXT,
    ADD COLUMN IF NOT EXISTS endpoint TEXT;

CREATE INDEX IF NOT EXISTS idx_logs_leitura_sub_base_username_created_at
    ON logs_leitura (sub_base, username, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_logs_leitura_sub_base_motoboy_created_at
    ON logs_leitura (sub_base, motoboy_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_logs_leitura_id_saida
    ON logs_leitura (id_saida);
