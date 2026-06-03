-- Controle de rotina de limpeza de histórico (D-60) e política de retenção.
-- Execute no banco do Render após deploy.

CREATE TABLE IF NOT EXISTS maintenance_job_state (
  job_name TEXT PRIMARY KEY,
  retention_days INTEGER NOT NULL DEFAULT 60,
  last_saida_id BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'idle',
  last_rows_historico INTEGER NOT NULL DEFAULT 0,
  last_rows_saidas INTEGER NOT NULL DEFAULT 0,
  last_duration_ms INTEGER NOT NULL DEFAULT 0,
  last_run_started_at TIMESTAMP NULL,
  last_run_finished_at TIMESTAMP NULL,
  last_error TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS history_retention_policy (
  id BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL DEFAULT '__global__',
  retention_days INTEGER NOT NULL DEFAULT 60,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_history_retention_policy_sub_base UNIQUE (sub_base)
);

-- Política global padrão v1 (sub_base='__global__' = regra default)
INSERT INTO history_retention_policy (sub_base, retention_days, ativo)
VALUES ('__global__', 60, TRUE)
ON CONFLICT (sub_base) DO UPDATE
SET retention_days = EXCLUDED.retention_days,
    ativo = EXCLUDED.ativo,
    updated_at = now();
