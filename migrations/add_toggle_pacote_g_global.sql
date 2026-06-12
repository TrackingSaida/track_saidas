ALTER TABLE entregador_preco_global
ADD COLUMN IF NOT EXISTS considerar_pacote_g_adicional BOOLEAN NOT NULL DEFAULT false;
