-- Migration compatível: sequence avulso, índice saidas_detail e permissão de digitação manual.
-- Seguro para rodar com backend antigo e novo.

-- 1) Sequence versionada de códigos avulso (antes era criada em runtime).
CREATE SEQUENCE IF NOT EXISTS avulso_codigo_seq START WITH 1 INCREMENT BY 1;

-- 2) Índice para obter o último detail da saída.
CREATE INDEX IF NOT EXISTS ix_saidas_detail_id_saida_id_detail_desc
  ON saidas_detail (id_saida, id_detail DESC);

-- 3) Permissão individual de digitação manual (default false).
ALTER TABLE motoboys
  ADD COLUMN IF NOT EXISTS pode_digitar_codigo_manual boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN motoboys.pode_digitar_codigo_manual IS
  'Quando true, motoboy pode digitar código manualmente no app; default false.';

-- Rollback (manual):
-- DROP INDEX IF EXISTS ix_saidas_detail_id_saida_id_detail_desc;
-- ALTER TABLE motoboys DROP COLUMN IF EXISTS pode_digitar_codigo_manual;
-- -- NÃO dropar avulso_codigo_seq em produção se já estiver em uso.
