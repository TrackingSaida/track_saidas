-- Cobrança de Owners v2: ajustes lançados dentro do período (antes do fechamento).
-- Aditiva e idempotente. Requer owner_cobranca_modulo.sql.
BEGIN;

ALTER TABLE owner_fechamento_ajuste ADD COLUMN IF NOT EXISTS periodo_inicio DATE NULL;
ALTER TABLE owner_fechamento_ajuste ADD COLUMN IF NOT EXISTS periodo_fim DATE NULL;
ALTER TABLE owner_fechamento_ajuste ADD COLUMN IF NOT EXISTS percentual NUMERIC(5, 2) NULL;
ALTER TABLE owner_fechamento_ajuste ADD COLUMN IF NOT EXISTS removido_em TIMESTAMP NULL;
ALTER TABLE owner_fechamento_ajuste ADD COLUMN IF NOT EXISTS removido_por_username TEXT NULL;

ALTER TABLE owner_fechamento_ajuste DROP CONSTRAINT IF EXISTS ck_owner_fechamento_ajuste_tipo;
ALTER TABLE owner_fechamento_ajuste
  ADD CONSTRAINT ck_owner_fechamento_ajuste_tipo
  CHECK (tipo IN ('acrescimo', 'desconto', 'credito', 'correcao', 'isencao'));

ALTER TABLE owner_fechamento_ajuste DROP CONSTRAINT IF EXISTS ck_owner_fechamento_ajuste_percentual;
ALTER TABLE owner_fechamento_ajuste
  ADD CONSTRAINT ck_owner_fechamento_ajuste_percentual
  CHECK (percentual IS NULL OR (percentual > 0 AND percentual <= 100));

CREATE INDEX IF NOT EXISTS ix_owner_fechamento_ajuste_periodo
  ON owner_fechamento_ajuste (owner_id, periodo_inicio, periodo_fim)
  WHERE periodo_inicio IS NOT NULL;

COMMIT;
