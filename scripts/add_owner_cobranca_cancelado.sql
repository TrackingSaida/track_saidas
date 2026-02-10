-- Adiciona coluna cancelado na tabela owner_cobranca_itens
-- Quando uma saida é alterada para status cancelado, o item de cobrança é marcado cancelado=true
-- e não será contabilizado nas agregações (dashboard, relatórios)
--
-- Executar: psql -d <database> -f scripts/add_owner_cobranca_cancelado.sql

ALTER TABLE owner_cobranca_itens
ADD COLUMN IF NOT EXISTS cancelado BOOLEAN NOT NULL DEFAULT false;
