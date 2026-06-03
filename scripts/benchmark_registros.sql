-- Benchmark base para comparar antes/depois da rotina D-60.
-- Ajuste o literal de sub_base e período conforme o ambiente.

EXPLAIN ANALYZE
SELECT id_saida, timestamp, codigo, status
FROM saidas
WHERE sub_base = 'SUA_SUB_BASE'
  AND timestamp >= (now() - interval '30 days')
ORDER BY timestamp DESC
LIMIT 50 OFFSET 0;

EXPLAIN ANALYZE
SELECT id
FROM saida_historico h
WHERE EXISTS (
  SELECT 1
  FROM saidas s
  WHERE s.id_saida = h.id_saida
    AND s.sub_base = 'SUA_SUB_BASE'
    AND s.timestamp >= (now() - interval '30 days')
)
ORDER BY id DESC
LIMIT 200;
