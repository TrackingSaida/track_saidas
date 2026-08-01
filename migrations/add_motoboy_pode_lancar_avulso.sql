-- Permissão de lançar avulso no app (motoboy). Default: pode (opt-out).
ALTER TABLE motoboys
  ADD COLUMN IF NOT EXISTS pode_lancar_avulso BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN motoboys.pode_lancar_avulso IS
  'Quando true, motoboy pode lançar avulso no app; default true (opt-out).';

-- Rollback (manual):
-- ALTER TABLE motoboys DROP COLUMN IF EXISTS pode_lancar_avulso;
