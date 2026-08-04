-- Foto obrigatória ao lançar avulso (motoboy). Default: off (opt-in).
ALTER TABLE motoboys
  ADD COLUMN IF NOT EXISTS avulso_exige_foto BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN motoboys.avulso_exige_foto IS
  'Quando true e pode_lancar_avulso, exige foto no lançamento de avulso; default false.';

-- Rollback (manual):
-- ALTER TABLE motoboys DROP COLUMN IF EXISTS avulso_exige_foto;
