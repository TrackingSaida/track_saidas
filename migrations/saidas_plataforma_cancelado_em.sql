-- Baixa automática de pedidos sincronizados: momento em que o Mercado Livre/Shopee informou
-- cancelamento de um pedido já coletado (o status no sistema não muda; a web sinaliza para
-- o operador verificar a devolução). Aditiva e idempotente. Executar antes do deploy.

ALTER TABLE saidas ADD COLUMN IF NOT EXISTS plataforma_cancelado_em TIMESTAMP WITHOUT TIME ZONE;
