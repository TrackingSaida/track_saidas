-- Permissão de motoboy para registrar entrada (default OFF).
-- Só vale operacionalmente com owner.entrada_obrigatoria_habilitada = true.

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS default_pode_realizar_entrada BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN owner.default_pode_realizar_entrada IS
  'Padrão para novos motoboys: pode registrar entrada no app (default false).';

ALTER TABLE motoboys
ADD COLUMN IF NOT EXISTS pode_realizar_entrada BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN motoboys.pode_realizar_entrada IS
  'Motoboy pode registrar entrada quando Controle de entrada do owner estiver ativo.';
