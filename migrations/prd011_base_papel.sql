-- PRD-011 Fatia 1: papel do cliente (seller | base_parceira).
-- Aditiva e idempotente.
--
--   psql "$DATABASE_URL" -f migrations/prd011_base_papel.sql

ALTER TABLE base
ADD COLUMN IF NOT EXISTS papel TEXT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_base_papel'
  ) THEN
    ALTER TABLE base
      ADD CONSTRAINT ck_base_papel
      CHECK (papel IS NULL OR papel IN ('seller', 'base_parceira'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_base_sub_base_papel ON base (sub_base, papel);

-- Backfill: só sub_base com exatamente um owner e tipo_owner = base.
UPDATE base b
SET papel = 'seller'
WHERE b.papel IS NULL
  AND btrim(COALESCE(b.sub_base, '')) <> ''
  AND (
    SELECT COUNT(*)
      FROM owner o
     WHERE btrim(COALESCE(o.sub_base, '')) = btrim(COALESCE(b.sub_base, ''))
  ) = 1
  AND EXISTS (
    SELECT 1
      FROM owner o
     WHERE btrim(COALESCE(o.sub_base, '')) = btrim(COALESCE(b.sub_base, ''))
       AND lower(btrim(COALESCE(o.tipo_owner, 'subbase'))) = 'base'
  );
