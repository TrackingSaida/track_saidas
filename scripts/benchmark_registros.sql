-- Benchmark da tela Registros (D-15) — GET /saidas/listar
-- Ajuste o literal de sub_base conforme o ambiente.
-- Executar em staging/produção com EXPLAIN (ANALYZE, BUFFERS).
--
-- Checklist de baseline (Etapa 0):
-- 1) X-Backend-Process-Time no header da resposta
-- 2) DevTools Network: duração, tamanho do payload, TTFB
-- 3) Logs Render: CPU/RAM e tempo do request
-- 4) Contagens abaixo + planos EXPLAIN
-- Não registrar códigos, cookies ou PII nos anexos de métrica.

-- Substitua antes de executar:
--   :sub_base  -> texto da sub_base
--   :de        -> date início (hoje - 15)
--   :ate_excl  -> date fim exclusivo (hoje + 1 dia)

\set sub_base 'SUA_SUB_BASE'

-- Cardinalidade D-15 (pré-filtro SQL aproximado)
SELECT
  COUNT(*) AS candidatas_timestamp_ou_hist
FROM saidas s
WHERE s.sub_base = :'sub_base'
  AND (
    (s.timestamp >= (CURRENT_DATE - INTERVAL '15 days')
     AND s.timestamp < (CURRENT_DATE + INTERVAL '1 day'))
    OR EXISTS (
      SELECT 1
      FROM saida_historico h
      WHERE h.id_saida = s.id_saida
        AND h.evento IN (
          'lido','scan','assumir','assumido','reatribuicao','reatribuido',
          'nova_saida_mesmo_entregador','lancar_avulso'
        )
        AND h.timestamp >= (CURRENT_DATE - INTERVAL '15 days')
        AND h.timestamp < (CURRENT_DATE + INTERVAL '1 day')
    )
  );

-- Volume de histórico associado às candidatas
SELECT COUNT(*) AS eventos_historico_candidatas
FROM saida_historico h
WHERE EXISTS (
  SELECT 1
  FROM saidas s
  WHERE s.id_saida = h.id_saida
    AND s.sub_base = :'sub_base'
    AND s.timestamp >= (CURRENT_DATE - INTERVAL '15 days')
);

-- Índices existentes
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('saidas', 'saida_historico')
ORDER BY tablename, indexname;

-- Plano: pré-filtro D-15 (ramo timestamp + EXISTS histórico)
EXPLAIN (ANALYZE, BUFFERS)
SELECT s.id_saida, s.timestamp, s.codigo, s.status, s.servico
FROM saidas s
WHERE s.sub_base = :'sub_base'
  AND (
    (s.timestamp >= (CURRENT_DATE - INTERVAL '15 days')
     AND s.timestamp < (CURRENT_DATE + INTERVAL '1 day'))
    OR EXISTS (
      SELECT 1
      FROM saida_historico h
      WHERE h.id_saida = s.id_saida
        AND h.evento IN (
          'lido','scan','assumir','assumido','reatribuicao','reatribuido',
          'nova_saida_mesmo_entregador','lancar_avulso'
        )
        AND h.timestamp >= (CURRENT_DATE - INTERVAL '15 days')
        AND h.timestamp < (CURRENT_DATE + INTERVAL '1 day')
    )
  );

-- Plano: página típica por timestamp (não equivale 100% à data operacional)
EXPLAIN (ANALYZE, BUFFERS)
SELECT id_saida, timestamp, codigo, status
FROM saidas
WHERE sub_base = :'sub_base'
  AND timestamp >= (CURRENT_DATE - INTERVAL '15 days')
  AND timestamp < (CURRENT_DATE + INTERVAL '1 day')
ORDER BY timestamp DESC, id_saida DESC
LIMIT 50 OFFSET 0;

-- Plano: lookup de histórico por lotes de id_saida
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, id_saida, evento, timestamp, user_id
FROM saida_historico
WHERE id_saida IN (
  SELECT id_saida
  FROM saidas
  WHERE sub_base = :'sub_base'
  ORDER BY timestamp DESC
  LIMIT 250
)
ORDER BY id_saida ASC, timestamp ASC, id ASC;
