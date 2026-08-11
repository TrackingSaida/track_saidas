-- Migração: campos de liquidação do fechamento do motoboy (status PAGO)
-- Executar manualmente no banco antes de subir a nova versão.
--
-- Exemplo:
--   psql "CONNECTION_STRING" -f migrations/entregador_fechamento_pago.sql

ALTER TABLE entregador_fechamentos
ADD COLUMN IF NOT EXISTS pago_em TIMESTAMP WITHOUT TIME ZONE NULL;

ALTER TABLE entregador_fechamentos
ADD COLUMN IF NOT EXISTS pago_por TEXT NULL;
