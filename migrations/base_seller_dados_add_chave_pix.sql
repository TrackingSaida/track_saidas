-- Migração: adiciona chave PIX opcional no cadastro da base.
-- É a chave para a base pagar o owner no fechamento; aparece no comprovante.
--
-- Exemplo de execução:
--   psql "CONNECTION_STRING" -f migrations/base_seller_dados_add_chave_pix.sql

ALTER TABLE base_seller_dados
ADD COLUMN IF NOT EXISTS chave_pix TEXT NULL;
