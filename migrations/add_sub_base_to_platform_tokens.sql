-- Adiciona coluna sub_base às tabelas de tokens de plataforma (ML e Shopee).
-- Permite associar cada token à sub_base que gerou o link e filtrar a listagem.
ALTER TABLE mercado_livre_tokens ADD COLUMN IF NOT EXISTS sub_base TEXT;
ALTER TABLE shopee_tokens ADD COLUMN IF NOT EXISTS sub_base TEXT;
