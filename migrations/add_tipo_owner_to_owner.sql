-- Adiciona coluna tipo_owner à tabela owner.
-- Valores: 'base' | 'subbase'. Default 'subbase' para owners existentes.
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS tipo_owner TEXT NOT NULL DEFAULT 'subbase';

COMMENT ON COLUMN owner.tipo_owner IS 'Tipo do owner: base (Seller) ou subbase (Base). Afeta labels no menu e páginas.';
