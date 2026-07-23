-- Permite devolução de pacotes à sub_base pelo app do entregador (com foto).
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS devolucao_sub_base_habilitada BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN owner.devolucao_sub_base_habilitada IS
  'Se true, motoboy pode devolver pacotes no app (foto obrigatória → status CANCELADO).';
