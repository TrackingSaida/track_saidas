-- ============================================================
-- Migração: Pacotes G (Grande) e ajustes no fechamento Base/Seller
-- - coletas.pacotes_g
-- - saidas.is_grande
-- - base_fechamentos: valor_adicao, motivo_adicao, valor_subtracao, motivo_subtracao
-- - base_fechamento_itens: pacotes_g, g_shopee, g_ml, g_avulso
-- ============================================================

-- Coletas: quantidade de pacotes grandes na coleta
ALTER TABLE coletas ADD COLUMN IF NOT EXISTS pacotes_g INTEGER NOT NULL DEFAULT 0;

-- Saidas: sinalização G (Grande) — única fonte para listagens e fechamentos
ALTER TABLE saidas ADD COLUMN IF NOT EXISTS is_grande BOOLEAN NOT NULL DEFAULT false;

-- Base Fechamentos: ajustes manuais (adição/subtração)
ALTER TABLE base_fechamentos ADD COLUMN IF NOT EXISTS valor_adicao NUMERIC(12, 2) NOT NULL DEFAULT 0.00;
ALTER TABLE base_fechamentos ADD COLUMN IF NOT EXISTS motivo_adicao TEXT;
ALTER TABLE base_fechamentos ADD COLUMN IF NOT EXISTS valor_subtracao NUMERIC(12, 2) NOT NULL DEFAULT 0.00;
ALTER TABLE base_fechamentos ADD COLUMN IF NOT EXISTS motivo_subtracao TEXT;

-- Base Fechamento Itens: quantidade G por dia (e por serviço)
ALTER TABLE base_fechamento_itens ADD COLUMN IF NOT EXISTS pacotes_g INTEGER NOT NULL DEFAULT 0;
ALTER TABLE base_fechamento_itens ADD COLUMN IF NOT EXISTS g_shopee INTEGER NOT NULL DEFAULT 0;
ALTER TABLE base_fechamento_itens ADD COLUMN IF NOT EXISTS g_ml INTEGER NOT NULL DEFAULT 0;
ALTER TABLE base_fechamento_itens ADD COLUMN IF NOT EXISTS g_avulso INTEGER NOT NULL DEFAULT 0;
