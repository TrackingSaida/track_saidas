-- PRD-004: autocadastro de motoboy por link de convite.
-- Aditiva e idempotente: não altera tabelas existentes.

CREATE TABLE IF NOT EXISTS motoboy_convite (
    id                  BIGSERIAL PRIMARY KEY,
    sub_base            TEXT        NOT NULL,
    token               TEXT        NOT NULL,
    criado_por_user_id  BIGINT      NULL,
    expira_em           TIMESTAMP   NOT NULL,
    limite_usos         INTEGER     NULL,
    ativo               BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_motoboy_convite_token
    ON motoboy_convite (token);

-- Só um convite ativo por sub_base.
CREATE UNIQUE INDEX IF NOT EXISTS uq_motoboy_convite_ativo_sub_base
    ON motoboy_convite (sub_base)
    WHERE ativo;

CREATE TABLE IF NOT EXISTS motoboy_cadastro_convite (
    id                      BIGSERIAL PRIMARY KEY,
    convite_id              BIGINT      NOT NULL REFERENCES motoboy_convite (id) ON DELETE CASCADE,
    sub_base                TEXT        NOT NULL,
    user_id                 BIGINT      NULL REFERENCES users (id) ON DELETE SET NULL,
    ip_origem               TEXT        NULL,
    aceite_privacidade_em   TIMESTAMP   NOT NULL,
    created_at              TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_motoboy_cadastro_convite_convite
    ON motoboy_cadastro_convite (convite_id);

CREATE INDEX IF NOT EXISTS ix_motoboy_cadastro_convite_ip_created
    ON motoboy_cadastro_convite (ip_origem, created_at);

COMMENT ON TABLE motoboy_convite IS 'Link de convite para autocadastro de motoboy (PRD-004).';
COMMENT ON TABLE motoboy_cadastro_convite IS 'Log de cadastros feitos por link de convite: auditoria, rate limit e aceite de privacidade.';
