-- Leitura da prévia de Cobrança de Owners (coletas por sub_base e janela de data).
-- Execute manualmente em janela de manutenção (CONCURRENTLY, fora de transação).
-- saidas (sub_base, codigo), saidas (sub_base, data, ...) e
-- saida_historico (id_saida, evento, timestamp) já existem em outras migrations.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_coletas_sub_base_timestamp
  ON coletas (sub_base, timestamp);
