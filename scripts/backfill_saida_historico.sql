-- Backfill saida_historico para saidas existentes (rodar uma vez).
-- 1) Uma linha "criado" por saída que ainda não tem nenhum histórico (usa saidas.timestamp).
-- 2) Uma linha "entregue" por saída que tem data_hora_entrega e ainda não tem evento entregue.

-- Saídas sem nenhum histórico: inserir evento origem
INSERT INTO saida_historico (id_saida, evento, timestamp, user_id)
SELECT s.id_saida, 'criado', s.timestamp, NULL
FROM saidas s
WHERE NOT EXISTS (SELECT 1 FROM saida_historico h WHERE h.id_saida = s.id_saida);

-- Saídas com data_hora_entrega sem evento entregue no histórico
INSERT INTO saida_historico (id_saida, evento, timestamp, user_id)
SELECT s.id_saida, 'entregue', s.data_hora_entrega, NULL
FROM saidas s
WHERE s.data_hora_entrega IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM saida_historico h WHERE h.id_saida = s.id_saida AND h.evento = 'entregue');
