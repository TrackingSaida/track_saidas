-- Adiciona coluna qr_payload_raw à tabela saidas (payload bruto do QR Mercado Livre)
-- Executar: psql -d <database> -f scripts/add_saidas_qr_payload_raw.sql
ALTER TABLE saidas ADD COLUMN IF NOT EXISTS qr_payload_raw TEXT;
