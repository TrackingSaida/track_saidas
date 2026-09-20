-- Número do pedido da loja/site (opcional) no envio próprio / etiqueta.
ALTER TABLE envio_proprio
  ADD COLUMN IF NOT EXISTS pedido_loja TEXT NULL;

COMMENT ON COLUMN envio_proprio.pedido_loja IS
  'Número do pedido no site/loja do seller (opcional); impresso na etiqueta sob o código RTE.';
