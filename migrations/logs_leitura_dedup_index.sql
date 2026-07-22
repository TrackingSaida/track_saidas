-- Índice para dedup rápido em registrar_log_leitura_critico (janela de poucos segundos).
-- Rodar em produção com CONCURRENTLY fora de transação.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_logs_leitura_dedup_critico
    ON logs_leitura (sub_base, username, tipo, resultado, codigo, id_saida, motoboy_id, created_at DESC);
