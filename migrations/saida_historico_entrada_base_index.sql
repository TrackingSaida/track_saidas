-- Acelera GET /entradas/resumo-dia (filtra evento=entrada_base + janela de timestamp).
-- Execute manualmente em janela de manutenção (CONCURRENTLY).

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saida_historico_entrada_base_ts
  ON saida_historico (timestamp)
  WHERE evento = 'entrada_base';
