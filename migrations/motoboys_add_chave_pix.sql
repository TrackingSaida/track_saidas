-- Migração: adiciona campo opcional de chave PIX no cadastro de motoboy.
-- Executar manualmente no banco antes de subir a nova versão.
--
-- Exemplo de execução:
--   psql "CONNECTION_STRING" -f migrations/motoboys_add_chave_pix.sql

ALTER TABLE motoboys
ADD COLUMN IF NOT EXISTS chave_pix TEXT NULL;
