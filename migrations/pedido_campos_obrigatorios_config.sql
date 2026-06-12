CREATE TABLE IF NOT EXISTS pedido_campos_obrigatorios_config (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    servico TEXT NOT NULL,
    contexto TEXT NOT NULL DEFAULT 'AMBOS',
    campos_obrigatorios TEXT NOT NULL DEFAULT '[]',
    ativo BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT ck_pedido_campos_obrigatorios_contexto
        CHECK (contexto IN ('ENTREGUE', 'AUSENTE', 'AMBOS'))
);

CREATE INDEX IF NOT EXISTS idx_pedido_campos_obrigatorios_sub_base_servico_ativo
    ON pedido_campos_obrigatorios_config (sub_base, servico, ativo);
