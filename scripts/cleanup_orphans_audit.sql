-- Auditoria de órfãos e volume elegível à rotina D-60 (v2).
-- Executar antes/depois do deploy para comparar.

SELECT count(*) AS saidas_detail_orfaos
FROM saidas_detail d
WHERE NOT EXISTS (SELECT 1 FROM saidas s WHERE s.id_saida = d.id_saida);

SELECT count(*) AS owner_cobranca_orfaos_por_saida
FROM owner_cobranca_itens o
WHERE o.id_saida IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM saidas s WHERE s.id_saida = o.id_saida);

SELECT count(*) AS coletas_orfas
FROM coletas c
WHERE NOT EXISTS (SELECT 1 FROM saidas s WHERE s.id_coleta = c.id_coleta);

-- Volume aproximado acima do cutoff global (60 dias default)
SELECT count(*) AS saidas_antigas
FROM saidas
WHERE timestamp < (now() - interval '60 days');

SELECT count(*) AS logs_leitura_antigos
FROM logs_leitura
WHERE created_at < (now() - interval '60 days');
