-- Digitação manual de código: padrão ativo (opt-out).
-- Ambientes que já rodaram avulso_comprovantes_ausencias.sql com DEFAULT false
-- precisam deste ajuste para novos cadastros e motoboys existentes.

ALTER TABLE motoboys
  ALTER COLUMN pode_digitar_codigo_manual SET DEFAULT true;

UPDATE motoboys
SET pode_digitar_codigo_manual = true
WHERE pode_digitar_codigo_manual IS DISTINCT FROM true;

COMMENT ON COLUMN motoboys.pode_digitar_codigo_manual IS
  'Quando true, motoboy pode digitar código manualmente no app; default true (opt-out).';

-- Rollback (manual):
-- ALTER TABLE motoboys ALTER COLUMN pode_digitar_codigo_manual SET DEFAULT false;
-- (não reverte o UPDATE em massa automaticamente)
