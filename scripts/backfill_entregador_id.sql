-- Backfill entregador_id nas saídas existentes
-- Rodar manualmente no banco após deploy das alterações de código
--
-- Preenche entregador_id nas saidas que têm entregador (nome) mas entregador_id NULL,
-- fazendo match por sub_base + nome do entregador na tabela entregador.

UPDATE saidas s
SET entregador_id = e.id_entregador
FROM entregador e
WHERE s.entregador = e.nome
  AND s.sub_base = e.sub_base
  AND s.entregador_id IS NULL;
