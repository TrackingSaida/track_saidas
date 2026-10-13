-- ML Int: vínculo da conta Mercado Livre com o seller cadastrado (tabela base).
-- Sem vínculo, o sync de envios não importa pedidos da conta.
-- Aditiva e idempotente. Executar antes do deploy (o modelo MLConexao já usa a coluna).

ALTER TABLE ml_conexoes ADD COLUMN IF NOT EXISTS id_base BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_ml_conexoes_id_base'
    ) THEN
        ALTER TABLE ml_conexoes
            ADD CONSTRAINT fk_ml_conexoes_id_base
            FOREIGN KEY (id_base) REFERENCES base(id_base) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_ml_conexoes_id_base ON ml_conexoes (id_base);
