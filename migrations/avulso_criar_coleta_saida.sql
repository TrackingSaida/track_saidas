-- Permissão de criar avulso por fluxo (Coleta vs Saída).
-- Backfill a partir de pode_lancar_avulso / default_pode_lancar_avulso.

ALTER TABLE motoboys
  ADD COLUMN IF NOT EXISTS pode_criar_avulso_coleta BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE motoboys
  ADD COLUMN IF NOT EXISTS pode_criar_avulso_saida BOOLEAN NOT NULL DEFAULT true;

UPDATE motoboys
SET
  pode_criar_avulso_coleta = COALESCE(pode_lancar_avulso, true),
  pode_criar_avulso_saida = COALESCE(pode_lancar_avulso, true)
WHERE TRUE;

COMMENT ON COLUMN motoboys.pode_criar_avulso_coleta IS
  'Quando true, motoboy pode criar avulso na Coleta.';
COMMENT ON COLUMN motoboys.pode_criar_avulso_saida IS
  'Quando true, motoboy pode criar avulso na Saída. Selecionar avulso não usa esta flag.';

ALTER TABLE owner
  ADD COLUMN IF NOT EXISTS default_pode_criar_avulso_coleta BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE owner
  ADD COLUMN IF NOT EXISTS default_pode_criar_avulso_saida BOOLEAN NOT NULL DEFAULT true;

UPDATE owner
SET
  default_pode_criar_avulso_coleta = COALESCE(default_pode_lancar_avulso, true),
  default_pode_criar_avulso_saida = COALESCE(default_pode_lancar_avulso, true)
WHERE TRUE;

COMMENT ON COLUMN owner.default_pode_criar_avulso_coleta IS
  'Padrão para novos motoboys: criar avulso na Coleta.';
COMMENT ON COLUMN owner.default_pode_criar_avulso_saida IS
  'Padrão para novos motoboys: criar avulso na Saída.';
