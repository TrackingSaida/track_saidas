-- Limite de participantes na coleta por owner: n (vários sem restrição) | um (um por vez, com Ajudar).
-- Default n: owners existentes passam a permitir entrada livre do 2º coletor.

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS coleta_participantes_modo TEXT NOT NULL DEFAULT 'n';

UPDATE owner
SET coleta_participantes_modo = 'n'
WHERE coleta_participantes_modo IS NULL
   OR trim(coleta_participantes_modo) = ''
   OR lower(trim(coleta_participantes_modo)) NOT IN ('n', 'um');

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'owner_coleta_participantes_modo_chk'
  ) THEN
    ALTER TABLE owner
    ADD CONSTRAINT owner_coleta_participantes_modo_chk
    CHECK (coleta_participantes_modo IN ('n', 'um'));
  END IF;
END $$;

COMMENT ON COLUMN owner.coleta_participantes_modo IS
  'Participantes na coleta: n = vários sem restrição; um = um por vez (409 + Ajudar).';
