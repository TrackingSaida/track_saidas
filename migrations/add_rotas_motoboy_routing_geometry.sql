-- Routing: metadata de otimização/geometria + tabela de idempotência
-- Deploy seguro com flags default OSRM (sem ativar Google).

ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS optimization_mode TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS geometry_provider TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS geometry_status TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS route_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS geometry_order_hash TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS polyline_encoded TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS distancia_total_m INTEGER;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS duracao_total_s INTEGER;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS optimization_input_hash TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS optimized_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS route_optimization_requests (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    route_id BIGINT NULL REFERENCES rotas_motoboy(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NULL,
    route_revision INTEGER NULL,
    status TEXT NOT NULL,
    response_json TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_route_opt_req_subbase_motoboy_key
        UNIQUE (sub_base, motoboy_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_route_opt_req_motoboy_created
    ON route_optimization_requests (motoboy_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_rotas_motoboy_revision
    ON rotas_motoboy (id, route_revision);
