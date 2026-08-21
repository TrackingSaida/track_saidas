-- Lifecycle operacional: reserva da base, colaboração e finalização explícita.
-- Aplicar depois de coleta_flexivel.sql.

BEGIN;

ALTER TABLE coleta_execucoes
    DROP CONSTRAINT IF EXISTS coleta_execucoes_status_check;
ALTER TABLE coleta_execucoes
    DROP CONSTRAINT IF EXISTS ck_coleta_execucoes_status;
ALTER TABLE coleta_execucoes
    ADD CONSTRAINT ck_coleta_execucoes_status
    CHECK (status IN ('em_coleta', 'coletado', 'sem_volume'));

ALTER TABLE coleta_execucao_participantes
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'finalizado';
ALTER TABLE coleta_execucao_participantes
    DROP CONSTRAINT IF EXISTS coleta_execucao_participantes_status_check;
ALTER TABLE coleta_execucao_participantes
    DROP CONSTRAINT IF EXISTS ck_coleta_participantes_status;
ALTER TABLE coleta_execucao_participantes
    ADD CONSTRAINT ck_coleta_participantes_status
    CHECK (status IN ('em_coleta', 'finalizado'));

ALTER TABLE coleta_execucao_participantes
    DROP CONSTRAINT IF EXISTS ck_coleta_participante_volume;
ALTER TABLE coleta_execucao_participantes
    ADD CONSTRAINT ck_coleta_participante_volume
    CHECK (status = 'em_coleta' OR sem_volume OR shopee + mercado_livre + avulso > 0);

COMMIT;
