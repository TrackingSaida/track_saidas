-- Histórico de eventos: colunas opcionais status_anterior / status_novo
ALTER TABLE saida_historico ADD COLUMN IF NOT EXISTS status_anterior TEXT;
ALTER TABLE saida_historico ADD COLUMN IF NOT EXISTS status_novo TEXT;

-- Índice para consultas por saida + tempo (histórico ordenado)
CREATE INDEX IF NOT EXISTS ix_saida_historico_id_saida_timestamp ON saida_historico(id_saida, timestamp);
