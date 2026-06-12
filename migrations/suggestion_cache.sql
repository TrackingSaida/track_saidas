CREATE TABLE IF NOT EXISTS suggestion_cache (
  id SERIAL PRIMARY KEY,
  key_hash VARCHAR(64) UNIQUE NOT NULL,
  sub_base VARCHAR(32) NOT NULL,
  query_normalizada TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  hit_count INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_suggestion_cache_updated ON suggestion_cache (updated_at);
CREATE INDEX IF NOT EXISTS idx_suggestion_cache_sub_base ON suggestion_cache (sub_base);
