-- PRD-011 Fatia 2: base parceira entrega pedidos, preço a pagar e repasse na saída.
-- Aditiva e idempotente.
--
--   psql "$DATABASE_URL" -f migrations/prd011_repasse_base.sql

ALTER TABLE base
ADD COLUMN IF NOT EXISTS entrega_pedidos BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS base_preco_pagar (
  id_base BIGINT PRIMARY KEY REFERENCES base(id_base) ON DELETE CASCADE,
  sub_base TEXT NOT NULL,
  shopee_valor NUMERIC(12, 2) NULL,
  ml_valor NUMERIC(12, 2) NULL,
  avulso_valor NUMERIC(12, 2) NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_base_preco_pagar_sub_base ON base_preco_pagar (sub_base);

ALTER TABLE saidas
ADD COLUMN IF NOT EXISTS repasse_base_id BIGINT NULL REFERENCES base(id_base) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS ix_saidas_repasse_base_id ON saidas (repasse_base_id)
WHERE repasse_base_id IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_saidas_destino_unico'
  ) THEN
    ALTER TABLE saidas
      ADD CONSTRAINT ck_saidas_destino_unico
      CHECK (NOT (motoboy_id IS NOT NULL AND repasse_base_id IS NOT NULL));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS base_repasse_fechamento (
  id_fechamento BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  id_base BIGINT NOT NULL REFERENCES base(id_base) ON DELETE RESTRICT,
  base TEXT NOT NULL,
  periodo_inicio DATE NOT NULL,
  periodo_fim DATE NOT NULL,
  valor_base NUMERIC(12, 2) NOT NULL DEFAULT 0,
  valor_adicao NUMERIC(12, 2) NOT NULL DEFAULT 0,
  motivo_adicao TEXT NULL,
  valor_subtracao NUMERIC(12, 2) NOT NULL DEFAULT 0,
  motivo_subtracao TEXT NULL,
  valor_final NUMERIC(12, 2) NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'GERADO',
  preco_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
  criado_em TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
  pago_em TIMESTAMP WITHOUT TIME ZONE NULL,
  pago_por TEXT NULL,
  CONSTRAINT uq_base_repasse_fechamento_periodo UNIQUE (sub_base, id_base, periodo_inicio, periodo_fim)
);

CREATE INDEX IF NOT EXISTS ix_base_repasse_fechamento_sub_periodo
  ON base_repasse_fechamento (sub_base, periodo_inicio, periodo_fim);

CREATE TABLE IF NOT EXISTS base_repasse_fechamento_itens (
  id_item BIGSERIAL PRIMARY KEY,
  id_fechamento BIGINT NOT NULL REFERENCES base_repasse_fechamento(id_fechamento) ON DELETE CASCADE,
  data DATE NOT NULL,
  shopee INTEGER NOT NULL DEFAULT 0,
  ml INTEGER NOT NULL DEFAULT 0,
  avulso INTEGER NOT NULL DEFAULT 0,
  cancelados INTEGER NOT NULL DEFAULT 0,
  valor_dia NUMERIC(12, 2) NOT NULL DEFAULT 0,
  CONSTRAINT uq_base_repasse_fechamento_item_data UNIQUE (id_fechamento, data)
);
