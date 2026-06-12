CREATE TABLE IF NOT EXISTS address_telemetry (
  id BIGSERIAL PRIMARY KEY,
  event_type VARCHAR(64) NOT NULL,
  sub_base VARCHAR(32),
  motoboy_id INT,
  query_hash VARCHAR(64),
  metadata JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_addr_telemetry_event ON address_telemetry (event_type, created_at);
