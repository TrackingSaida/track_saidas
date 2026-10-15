-- Contagem nos apps (Flex e Shopee) no fechamento do motoboy. Guarda a quantidade informada,
-- a do sistema e o preço usado; só a diferença entra no valor final. Aditiva e idempotente.
-- Executar antes do deploy.

ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS qtd_app_flex INTEGER;
ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS qtd_app_shopee INTEGER;
ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS qtd_sistema_flex INTEGER;
ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS qtd_sistema_shopee INTEGER;
ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS preco_acerto_flex NUMERIC(12, 2);
ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS preco_acerto_shopee NUMERIC(12, 2);
ALTER TABLE entregador_fechamentos ADD COLUMN IF NOT EXISTS valor_acerto_apps NUMERIC(12, 2) NOT NULL DEFAULT 0.00;
