-- Refresh tokens para sessão mobile de motoboys (idempotente)

CREATE TABLE IF NOT EXISTS motoboy_refresh_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_motoboy_refresh_token_hash
    ON motoboy_refresh_tokens (token_hash);

CREATE INDEX IF NOT EXISTS ix_motoboy_refresh_user_active
    ON motoboy_refresh_tokens (user_id)
    WHERE revoked_at IS NULL;
