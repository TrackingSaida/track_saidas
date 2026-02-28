-- ML Int: tabela ml_conexoes e colunas em saidas / saidas_detail
-- Executar uma vez no banco.

-- Tabela de conexões seller ↔ transportadora (sub_base)
CREATE TABLE IF NOT EXISTS ml_conexoes (
    id SERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    user_id_ml BIGINT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    atualizado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_ml_conexoes_sub_base ON ml_conexoes(sub_base);
CREATE INDEX IF NOT EXISTS ix_ml_conexoes_user_id_ml ON ml_conexoes(user_id_ml);

-- Saida: vínculo com envio ML (evita duplicata no auto-fill)
ALTER TABLE saidas ADD COLUMN IF NOT EXISTS ml_shipment_id BIGINT;
ALTER TABLE saidas ADD COLUMN IF NOT EXISTS ml_order_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_saidas_ml_shipment_id ON saidas(ml_shipment_id) WHERE ml_shipment_id IS NOT NULL;

-- SaidaDetail: id_entregador nullable para status "Aguardando coleta"
ALTER TABLE saidas_detail ALTER COLUMN id_entregador DROP NOT NULL;
