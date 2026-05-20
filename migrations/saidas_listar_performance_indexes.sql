-- Índices para melhorar listagem de registros em alto volume
-- Execute manualmente em janela de manutenção (usa CONCURRENTLY).

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saidas_sub_base_timestamp
  ON saidas (sub_base, timestamp DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saidas_sub_base_codigo
  ON saidas (sub_base, codigo)
  WHERE codigo IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saidas_sub_base_motoboy_ts
  ON saidas (sub_base, motoboy_id, timestamp DESC)
  WHERE motoboy_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saida_historico_saida_ts
  ON saida_historico (id_saida, timestamp DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saida_historico_evento_ts
  ON saida_historico (evento, timestamp DESC);
