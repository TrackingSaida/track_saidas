-- PRD-012 Fase 3: persiste a chave idempotente da confirmação de saída.
-- Aditiva. Aplicar antes de ligar confirmacao_saida_sessoes_habilitada.
-- Não foi executada neste passo.

BEGIN;

CREATE TABLE IF NOT EXISTS saida_confirmacao_idempotencia (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NULL,
    sub_base TEXT NOT NULL,
    motoboy_id BIGINT NOT NULL,
    chave TEXT NOT NULL,
    resultado_json TEXT NOT NULL,
    criada_em TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_saida_confirmacao_idem_sub_motoboy_chave UNIQUE (sub_base, motoboy_id, chave)
);

COMMIT;
