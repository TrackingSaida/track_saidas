-- Adiciona modo_operacao ao owner e origem às coletas (Coleta Manual)
-- Executar: psql -d <database> -f scripts/add_owner_modo_operacao_coleta_origem.sql

-- Owner: modo_operacao = 'codigo' (padrão) ou 'coleta_manual'
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS modo_operacao TEXT DEFAULT 'codigo';

-- Coleta: origem = 'codigo' (padrão) ou 'manual'
ALTER TABLE coletas
ADD COLUMN IF NOT EXISTS origem TEXT NOT NULL DEFAULT 'codigo';
