-- Migração: Rota ativa persistida (rotas_motoboy)
-- Executar manualmente no banco antes de usar os endpoints /mobile/rotas/*

CREATE TABLE IF NOT EXISTS rotas_motoboy (
    id BIGSERIAL PRIMARY KEY,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    data DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'ativa',
    ordem_json TEXT NOT NULL,
    parada_atual INTEGER NOT NULL DEFAULT 0,
    iniciado_em TIMESTAMP,
    finalizado_em TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_rotas_motoboy_motoboy_id ON rotas_motoboy(motoboy_id);
CREATE INDEX IF NOT EXISTS ix_rotas_motoboy_status_data ON rotas_motoboy(motoboy_id, status, data);
