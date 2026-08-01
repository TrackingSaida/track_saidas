-- Push notifications: tokens, preferências, digest, log de envio e avisos da base.
-- Também adiciona colunas de PDF no fechamento de entregador/motoboy.

CREATE TABLE IF NOT EXISTS device_push_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    motoboy_id BIGINT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    role INTEGER NOT NULL,
    sub_base TEXT NOT NULL,
    expo_push_token TEXT NOT NULL,
    platform TEXT NULL,
    ativo BOOLEAN NOT NULL DEFAULT true,
    criado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_device_push_tokens_token UNIQUE (expo_push_token)
);

CREATE INDEX IF NOT EXISTS ix_device_push_tokens_motoboy_sub
    ON device_push_tokens (motoboy_id, sub_base) WHERE ativo = true AND motoboy_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_device_push_tokens_staff_sub
    ON device_push_tokens (sub_base, role) WHERE ativo = true AND motoboy_id IS NULL;

CREATE TABLE IF NOT EXISTS notif_prefs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    motoboy_id BIGINT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    sub_base TEXT NOT NULL,
    -- motoboy
    fechamento BOOLEAN NOT NULL DEFAULT true,
    pacotes_atribuidos BOOLEAN NOT NULL DEFAULT true,
    atraso_d1 BOOLEAN NOT NULL DEFAULT true,
    avisos_base BOOLEAN NOT NULL DEFAULT true,
    -- staff
    reconferir_saida BOOLEAN NOT NULL DEFAULT true,
    criado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_notif_prefs_user_sub UNIQUE (user_id, sub_base)
);

CREATE TABLE IF NOT EXISTS push_digest (
    id BIGSERIAL PRIMARY KEY,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    sub_base TEXT NOT NULL,
    tipo TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_codigo TEXT NULL,
    flush_after TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    criado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    atualizado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_push_digest_motoboy_sub_tipo UNIQUE (motoboy_id, sub_base, tipo)
);

CREATE TABLE IF NOT EXISTS push_envio_log (
    id BIGSERIAL PRIMARY KEY,
    destinatario_tipo TEXT NOT NULL,
    destinatario_id BIGINT NOT NULL,
    sub_base TEXT NOT NULL,
    tipo TEXT NOT NULL,
    chave_dedupe TEXT NOT NULL,
    enviado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_push_envio_log_dedupe UNIQUE (destinatario_tipo, destinatario_id, sub_base, tipo, chave_dedupe)
);

CREATE TABLE IF NOT EXISTS avisos_base (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    criado_por BIGINT NULL REFERENCES users(id) ON DELETE SET NULL,
    titulo TEXT NOT NULL,
    mensagem TEXT NOT NULL,
    prioridade TEXT NOT NULL DEFAULT 'normal',
    criado_em TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_avisos_base_prioridade CHECK (prioridade IN ('normal', 'urgente'))
);

CREATE INDEX IF NOT EXISTS ix_avisos_base_sub_criado
    ON avisos_base (sub_base, criado_em DESC);

CREATE TABLE IF NOT EXISTS aviso_destinatarios (
    id BIGSERIAL PRIMARY KEY,
    aviso_id BIGINT NOT NULL REFERENCES avisos_base(id) ON DELETE CASCADE,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    lido_em TIMESTAMP WITHOUT TIME ZONE NULL,
    CONSTRAINT uq_aviso_destinatario UNIQUE (aviso_id, motoboy_id)
);

CREATE INDEX IF NOT EXISTS ix_aviso_destinatarios_motoboy_lido
    ON aviso_destinatarios (motoboy_id, lido_em);

ALTER TABLE entregador_fechamentos
    ADD COLUMN IF NOT EXISTS pdf_object_key TEXT NULL;

ALTER TABLE entregador_fechamentos
    ADD COLUMN IF NOT EXISTS pdf_gerado_em TIMESTAMP WITHOUT TIME ZONE NULL;

COMMENT ON TABLE device_push_tokens IS 'Tokens Expo Push por usuário/device (motoboy e staff).';
COMMENT ON TABLE avisos_base IS 'Avisos manuais da base para motoboys.';
