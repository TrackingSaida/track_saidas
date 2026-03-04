-- Adiciona coluna nome_fantasia à tabela owner (opcional, para "Emitido por" no relatório).
-- Não afeta registros atuais. Não altera índices existentes.
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS nome_fantasia TEXT NULL;

COMMENT ON COLUMN owner.nome_fantasia IS 'Nome institucional do emissor para relatórios (ex.: razão social). Opcional.';
