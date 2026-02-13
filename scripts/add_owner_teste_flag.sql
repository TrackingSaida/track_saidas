-- Adiciona flag de teste para owners
-- Owners marcados como teste não são considerados em dashboards/admin
-- Executar: psql -d <database> -f scripts/add_owner_teste_flag.sql

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS teste BOOLEAN NOT NULL DEFAULT false;

