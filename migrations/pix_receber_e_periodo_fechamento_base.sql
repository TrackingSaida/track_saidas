-- PRD-009: PIX a receber da operação e período de fechamento da base.
-- Aditiva e idempotente. Sem ela, políticas e o cadastro da base respondem 500
-- porque o modelo já usa as colunas.
--
--   psql "$DATABASE_URL" -f migrations/pix_receber_e_periodo_fechamento_base.sql

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS pix_receber TEXT NULL;

ALTER TABLE base
ADD COLUMN IF NOT EXISTS periodo_fechamento TEXT NULL;
