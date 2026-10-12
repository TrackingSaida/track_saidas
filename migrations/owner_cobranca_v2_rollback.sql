-- Rollback de owner_cobranca_v2.sql. Apaga os ajustes lançados dentro do período.
-- Só executar com confirmação explícita.
BEGIN;

DELETE FROM owner_fechamento_ajuste WHERE periodo_inicio IS NOT NULL OR tipo = 'isencao';

DROP INDEX IF EXISTS ix_owner_fechamento_ajuste_periodo;
ALTER TABLE owner_fechamento_ajuste DROP CONSTRAINT IF EXISTS ck_owner_fechamento_ajuste_percentual;
ALTER TABLE owner_fechamento_ajuste DROP CONSTRAINT IF EXISTS ck_owner_fechamento_ajuste_tipo;
ALTER TABLE owner_fechamento_ajuste
  ADD CONSTRAINT ck_owner_fechamento_ajuste_tipo
  CHECK (tipo IN ('acrescimo', 'desconto', 'credito', 'correcao'));

ALTER TABLE owner_fechamento_ajuste DROP COLUMN IF EXISTS removido_por_username;
ALTER TABLE owner_fechamento_ajuste DROP COLUMN IF EXISTS removido_em;
ALTER TABLE owner_fechamento_ajuste DROP COLUMN IF EXISTS percentual;
ALTER TABLE owner_fechamento_ajuste DROP COLUMN IF EXISTS periodo_fim;
ALTER TABLE owner_fechamento_ajuste DROP COLUMN IF EXISTS periodo_inicio;

COMMIT;
