-- Cache de geocoding (idempotente)
CREATE TABLE IF NOT EXISTS geocode_cache (
  id SERIAL PRIMARY KEY,
  key_hash VARCHAR(64) UNIQUE NOT NULL,
  query_normalizada TEXT NOT NULL,
  latitude DOUBLE PRECISION NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  provider TEXT,
  confidence REAL,
  hit_count INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_geocode_cache_updated_at ON geocode_cache (updated_at);
