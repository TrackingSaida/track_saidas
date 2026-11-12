BEGIN;

ALTER TABLE owner
    ADD COLUMN IF NOT EXISTS confirmacao_saida_sessoes_habilitada boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS saida_sessoes (
    id bigserial PRIMARY KEY,
    owner_id bigint REFERENCES owner(id_owner) ON DELETE SET NULL,
    sub_base text NOT NULL,
    motoboy_id bigint NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    data_ref date NOT NULL,
    estado text NOT NULL DEFAULT 'ABERTA' CHECK (estado IN ('ABERTA', 'FINALIZADA', 'CANCELADA')),
    versao integer NOT NULL DEFAULT 1,
    aberta_por_user_id bigint,
    finalizada_por_user_id bigint,
    cancelada_por_user_id bigint,
    confirmada_por_user_id bigint,
    criada_em timestamp without time zone NOT NULL DEFAULT now(),
    atualizada_em timestamp without time zone NOT NULL DEFAULT now(),
    finalizada_em timestamp without time zone,
    cancelada_em timestamp without time zone,
    confirmada_em timestamp without time zone,
    motivo_cancelamento text,
    total_snapshot integer NOT NULL DEFAULT 0,
    total_shopee_snapshot integer NOT NULL DEFAULT 0,
    total_ml_snapshot integer NOT NULL DEFAULT 0,
    total_avulso_snapshot integer NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_saida_sessao_aberta_motoboy
    ON saida_sessoes(sub_base, motoboy_id)
    WHERE estado = 'ABERTA';
CREATE INDEX IF NOT EXISTS ix_saida_sessoes_pendentes
    ON saida_sessoes(sub_base, motoboy_id, estado, confirmada_em);

CREATE TABLE IF NOT EXISTS saida_sessao_itens (
    id bigserial PRIMARY KEY,
    sessao_id bigint NOT NULL REFERENCES saida_sessoes(id) ON DELETE CASCADE,
    id_saida bigint NOT NULL,
    codigo_snapshot text,
    servico_snapshot text,
    status_snapshot text,
    incluido_por_user_id bigint,
    incluido_em timestamp without time zone NOT NULL DEFAULT now(),
    ativo_operacional boolean NOT NULL DEFAULT true,
    removido_em timestamp without time zone,
    removido_por_user_id bigint,
    motivo_remocao text,
    CONSTRAINT uq_saida_sessao_item_sessao_saida UNIQUE (sessao_id, id_saida)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_saida_sessao_item_ciclo_ativo
    ON saida_sessao_itens(id_saida)
    WHERE ativo_operacional = true;
CREATE INDEX IF NOT EXISTS ix_saida_sessao_itens_sessao_ativos
    ON saida_sessao_itens(sessao_id, ativo_operacional);

ALTER TABLE conferencia_saida ADD COLUMN IF NOT EXISTS sessao_id bigint;
ALTER TABLE conferencia_saida
    DROP CONSTRAINT IF EXISTS uq_conferencia_saida_sub_motoboy_dia;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_conferencia_saida_sessao'
    ) THEN
        ALTER TABLE conferencia_saida
            ADD CONSTRAINT fk_conferencia_saida_sessao
            FOREIGN KEY (sessao_id) REFERENCES saida_sessoes(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_conferencia_saida_sessao
    ON conferencia_saida(sessao_id) WHERE sessao_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conferencia_saida_legado_dia
    ON conferencia_saida(sub_base, motoboy_id, data_ref) WHERE sessao_id IS NULL;

COMMIT;
