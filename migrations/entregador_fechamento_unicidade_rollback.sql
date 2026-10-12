-- Rollback de entregador_fechamento_unicidade.sql (remove só os índices; não apaga dados).
DROP INDEX IF EXISTS uq_entregador_fechamento_motoboy_periodo;
DROP INDEX IF EXISTS uq_entregador_fechamento_entregador_periodo;
