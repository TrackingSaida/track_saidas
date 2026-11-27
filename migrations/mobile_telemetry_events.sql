-- Telemetria leve do funil de baixa mobile (PRD-018). Retenção curta via cleanup.
CREATE TABLE IF NOT EXISTS mobile_telemetry_events (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  event_at TIMESTAMPTZ NULL,
  event_name TEXT NOT NULL,
  motoboy_id BIGINT NOT NULL,
  sub_base TEXT NOT NULL,
  owner_id BIGINT NULL,
  id_saida BIGINT NULL,
  client_action_id TEXT NULL,
  duration_ms INT NULL,
  stage TEXT NULL,
  app_version TEXT NULL,
  platform TEXT NULL,
  device_brand TEXT NULL,
  device_manufacturer TEXT NULL,
  device_model TEXT NULL,
  os_name TEXT NULL,
  os_version TEXT NULL,
  android_api_level INT NULL,
  device_total_memory_mb INT NULL,
  cpu_arch TEXT NULL,
  props_json JSONB NULL
);

CREATE INDEX IF NOT EXISTS idx_mobile_telemetry_sub_event_created
  ON mobile_telemetry_events (sub_base, event_name, created_at);

CREATE INDEX IF NOT EXISTS idx_mobile_telemetry_motoboy_created
  ON mobile_telemetry_events (motoboy_id, created_at);

CREATE INDEX IF NOT EXISTS idx_mobile_telemetry_client_action
  ON mobile_telemetry_events (client_action_id)
  WHERE client_action_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mobile_telemetry_sub_model_created
  ON mobile_telemetry_events (sub_base, device_model, created_at);

CREATE INDEX IF NOT EXISTS idx_mobile_telemetry_sub_api_created
  ON mobile_telemetry_events (sub_base, android_api_level, created_at);
