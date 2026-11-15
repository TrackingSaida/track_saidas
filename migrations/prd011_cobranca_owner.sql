-- PRD-011 Fatia 3: cobrança do owner com dois preços (sellers / bases), data de parceria
-- e prazo de faturamento. Aditiva e idempotente.
--
--   psql "$DATABASE_URL" -f migrations/prd011_cobranca_owner.sql

BEGIN;

-- Data de parceria: imutável depois de preenchida (regra na API). Legado fica nulo.
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS criado_em DATE NULL;

-- Faturar depois de N dias do fim do período.
ALTER TABLE owner_configuracao_cobranca
ADD COLUMN IF NOT EXISTS dias_faturamento INTEGER NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_owner_config_cobranca_dias_faturamento'
  ) THEN
    ALTER TABLE owner_configuracao_cobranca
      ADD CONSTRAINT ck_owner_config_cobranca_dias_faturamento
      CHECK (dias_faturamento IS NULL OR dias_faturamento >= 0);
  END IF;
END $$;

-- Preço por escopo: 'bases' é o preço que já existia; 'sellers' é o novo.
ALTER TABLE owner_preco_vigencia
ADD COLUMN IF NOT EXISTS escopo TEXT NOT NULL DEFAULT 'bases';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_owner_preco_vigencia_escopo'
  ) THEN
    ALTER TABLE owner_preco_vigencia
      ADD CONSTRAINT ck_owner_preco_vigencia_escopo CHECK (escopo IN ('bases', 'sellers'));
  END IF;
END $$;

ALTER TABLE owner_preco_vigencia
DROP CONSTRAINT IF EXISTS uq_owner_preco_vigencia_inicio;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'uq_owner_preco_vigencia_escopo_inicio'
  ) THEN
    ALTER TABLE owner_preco_vigencia
      ADD CONSTRAINT uq_owner_preco_vigencia_escopo_inicio UNIQUE (owner_id, escopo, inicio);
  END IF;
END $$;

-- Fechamento: data de faturamento e snapshot dos dois preços (nulos nos fechamentos antigos).
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS data_faturamento DATE NULL;
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS qtd_sellers INTEGER NULL;
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS preco_sellers NUMERIC(12, 4) NULL;
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS valor_sellers NUMERIC(14, 2) NULL;
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS qtd_bases INTEGER NULL;
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS preco_bases NUMERIC(12, 4) NULL;
ALTER TABLE owner_fechamento ADD COLUMN IF NOT EXISTS valor_bases NUMERIC(14, 2) NULL;

COMMIT;
