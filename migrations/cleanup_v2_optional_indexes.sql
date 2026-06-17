-- Índices opcionais para Fase B da limpeza D-60 v2.
-- Executar após deploy se deletes por data ficarem lentos.

CREATE INDEX IF NOT EXISTS idx_rotas_motoboy_data_status ON rotas_motoboy (data, status);
CREATE INDEX IF NOT EXISTS idx_logs_leitura_created_at ON logs_leitura (created_at);
