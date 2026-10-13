-- ML Int: Pack ID do Mercado Livre (exibido na etiqueta) na saída importada.
-- Aditiva e idempotente. Executar antes do deploy (o modelo Saida já usa a coluna).

ALTER TABLE saidas ADD COLUMN IF NOT EXISTS ml_pack_id BIGINT;
