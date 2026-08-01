-- Conferência de saída por motoboy + dia operacional (controle operacional).
CREATE TABLE IF NOT EXISTS conferencia_saida (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    owner_id BIGINT NULL REFERENCES owner(id_owner) ON DELETE SET NULL,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    data_ref DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendente',
    conferido_por BIGINT NULL,
    conferido_em TIMESTAMP WITHOUT TIME ZONE NULL,
    ultima_abertura_em TIMESTAMP WITHOUT TIME ZONE NULL,
    qtd_no_momento INTEGER NULL,
    CONSTRAINT uq_conferencia_saida_sub_motoboy_dia UNIQUE (sub_base, motoboy_id, data_ref),
    CONSTRAINT ck_conferencia_saida_status CHECK (status IN ('pendente', 'reconferir', 'conferida'))
);

CREATE INDEX IF NOT EXISTS ix_conferencia_saida_sub_data_status
    ON conferencia_saida (sub_base, data_ref, status);

CREATE INDEX IF NOT EXISTS ix_conferencia_saida_sub_motoboy_data
    ON conferencia_saida (sub_base, motoboy_id, data_ref);

COMMENT ON TABLE conferencia_saida IS
  'Conferência operacional da saída do motoboy no dia (não altera Saida.status).';
