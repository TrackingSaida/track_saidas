-- Auditoria de remoção de leituras de coleta operacional.
-- Aplicar antes de subir a versão com DELETE /coletas/operacionais/leituras/{id_saida}.

BEGIN;

CREATE TABLE IF NOT EXISTS coleta_leitura_remocoes (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    base_id BIGINT NULL,
    base TEXT NOT NULL,
    data_operacao DATE NOT NULL,
    id_saida BIGINT NOT NULL,
    codigo TEXT NOT NULL,
    servico TEXT NULL,
    operador_user_id BIGINT NULL,
    operador_username TEXT NULL,
    removido_por_user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    removido_por_username TEXT NOT NULL,
    motivo TEXT NULL,
    criado_em TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_coleta_leitura_remocoes_sub_saida
    ON coleta_leitura_remocoes (sub_base, id_saida);

CREATE INDEX IF NOT EXISTS ix_coleta_leitura_remocoes_sub_base_data
    ON coleta_leitura_remocoes (sub_base, base_id, data_operacao);

-- Índice candidato para listagem paginada de leituras (validar com EXPLAIN antes em produção).
CREATE INDEX IF NOT EXISTS ix_saidas_coleta_leitura_listagem
    ON saidas (sub_base, data, id_coleta, timestamp DESC, id_saida DESC)
    WHERE id_coleta IS NOT NULL;

COMMIT;
