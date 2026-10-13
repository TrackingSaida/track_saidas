-- Shopee: vínculo da conta Shopee com o seller cadastrado (tabela base) e nº do pedido
-- Shopee (order_sn) na saída importada. Sem vínculo, o sync não importa pedidos da loja.
-- Aditiva e idempotente. Executar antes do deploy (os modelos já usam as colunas).

ALTER TABLE shopee_tokens ADD COLUMN IF NOT EXISTS id_base BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_shopee_tokens_id_base'
    ) THEN
        ALTER TABLE shopee_tokens
            ADD CONSTRAINT fk_shopee_tokens_id_base
            FOREIGN KEY (id_base) REFERENCES base(id_base) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_shopee_tokens_id_base ON shopee_tokens (id_base);

ALTER TABLE saidas ADD COLUMN IF NOT EXISTS shopee_order_sn TEXT;

CREATE INDEX IF NOT EXISTS ix_saidas_sub_base_shopee_order_sn
    ON saidas (sub_base, shopee_order_sn)
    WHERE shopee_order_sn IS NOT NULL;
