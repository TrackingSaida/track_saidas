BEGIN;

CREATE TABLE IF NOT EXISTS saida_transferencias_pendentes (
    id bigserial PRIMARY KEY,
    id_saida bigint NOT NULL,
    sub_base text NOT NULL,
    motoboy_origem_id bigint NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    motoboy_destino_id bigint REFERENCES motoboys(id_motoboy) ON DELETE SET NULL,
    repasse_base_id bigint,
    solicitante_user_id bigint,
    solicitante_motoboy_id bigint REFERENCES motoboys(id_motoboy) ON DELETE SET NULL,
    rota_id bigint,
    route_revision_ref integer,
    status text NOT NULL DEFAULT 'pendente'
        CHECK (status IN ('pendente', 'aceita', 'recusada', 'cancelada', 'obsoleta')),
    criada_em timestamp without time zone NOT NULL DEFAULT now(),
    resolvida_em timestamp without time zone,
    resolvida_por_user_id bigint
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_saida_transferencia_pendente_saida
    ON saida_transferencias_pendentes(id_saida)
    WHERE status = 'pendente';

CREATE INDEX IF NOT EXISTS ix_saida_transferencias_origem_status
    ON saida_transferencias_pendentes(motoboy_origem_id, status);

CREATE INDEX IF NOT EXISTS ix_saida_transferencias_sub_base_status
    ON saida_transferencias_pendentes(sub_base, status);

COMMIT;
