-- Metadados de geocoding em saidas_detail (fonte, score, timestamp)
ALTER TABLE saidas_detail ADD COLUMN IF NOT EXISTS geocode_source TEXT NULL;
ALTER TABLE saidas_detail ADD COLUMN IF NOT EXISTS geocode_score NUMERIC(5, 2) NULL;
ALTER TABLE saidas_detail ADD COLUMN IF NOT EXISTS geocoded_at TIMESTAMP NULL;
