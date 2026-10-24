-- Tipo da chave PIX a receber (políticas e ficha da base).
-- Aditiva e idempotente. Sem ela a API responde 500 em Políticas gerais,
-- no cadastro da base e no fechamento de coletas, porque o modelo já usa as colunas.
--
--   psql "$DATABASE_URL" -f migrations/pix_receber_tipo_chave.sql

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS pix_receber_tipo TEXT NULL;

ALTER TABLE base_seller_dados
ADD COLUMN IF NOT EXISTS chave_pix_tipo TEXT NULL;
