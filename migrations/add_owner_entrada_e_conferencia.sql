-- Flags operacionais por owner (default off).
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS entrada_obrigatoria_habilitada BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS conferencia_saida_habilitada BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN owner.entrada_obrigatoria_habilitada IS
  'Se true, pacote só pode sair após Registrar Entrada (status NA_BASE).';

COMMENT ON COLUMN owner.conferencia_saida_habilitada IS
  'Se true, habilita Conferência de Saída (pendente/reconferir/conferida) após Começar Entrega.';
