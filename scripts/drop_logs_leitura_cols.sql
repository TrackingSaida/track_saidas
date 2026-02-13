-- Remove colunas não utilizadas da tabela logs_leitura
-- Executar: psql -d <database> -f scripts/drop_logs_leitura_cols.sql

ALTER TABLE logs_leitura DROP COLUMN IF EXISTS front_processing_ms;
ALTER TABLE logs_leitura DROP COLUMN IF EXISTS front_network_ms;
ALTER TABLE logs_leitura DROP COLUMN IF EXISTS front_total_ms;
ALTER TABLE logs_leitura DROP COLUMN IF EXISTS request_id;
ALTER TABLE logs_leitura DROP COLUMN IF EXISTS attempt;
