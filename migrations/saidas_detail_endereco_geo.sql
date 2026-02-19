-- Migração: Campos de endereço/geo em saidas_detail (fluxo profissional motoboy)
-- Executar manualmente no banco antes de subir a nova versão

-- saidas_detail: latitude, longitude, endereco_formatado, endereco_origem
ALTER TABLE saidas_detail
ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION,
ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION,
ADD COLUMN IF NOT EXISTS endereco_formatado TEXT,
ADD COLUMN IF NOT EXISTS endereco_origem TEXT;

COMMENT ON COLUMN saidas_detail.endereco_origem IS 'manual | ocr | voz';
