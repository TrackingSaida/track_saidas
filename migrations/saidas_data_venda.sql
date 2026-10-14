-- Data da venda no Mercado Livre/Shopee (horário de Brasília) dos pedidos sincronizados,
-- exibida no detalhe do pedido. Aditiva e idempotente. Executar antes do deploy.

ALTER TABLE saidas ADD COLUMN IF NOT EXISTS data_venda TIMESTAMP WITHOUT TIME ZONE;
