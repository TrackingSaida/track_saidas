-- Recoloca no dia operacional as coletas criadas por transferência de base.
--
-- A transferência gravava coletas.timestamp com o horário de agora. A consulta
-- soma a execução do dia; o fechamento soma a coleta por coletas.timestamp.
-- O volume transferido sumia da quinzena e podia aparecer no dia da transferência.
--
-- Este script só altera a data. Não mexe em quantidade, base, valor nem status.
-- Idempotente: coleta já no dia certo não é atualizada.
-- Coleta com pacotes de mais de um dia não é alterada; a consulta no final lista esses casos.
--
-- Rode primeiro o SELECT de conferência (arquivo, bloco 1). Só então aplique o UPDATE.

-- 1) Conferência — o que será corrigido
SELECT
    c.sub_base,
    c.id_coleta,
    c.base,
    c.timestamp AS data_gravada,
    s.data_op AS dia_operacional,
    c.shopee,
    c.mercado_livre,
    c.avulso,
    c.valor_total,
    s.qtd_saidas
FROM coletas c
JOIN (
    SELECT
        sa.id_coleta,
        sa.sub_base,
        MIN(sa.data) AS data_op,
        COUNT(DISTINCT sa.data) AS dias,
        COUNT(*) AS qtd_saidas
    FROM saidas sa
    JOIN (
        SELECT DISTINCT sa2.id_coleta
        FROM saidas sa2
        JOIN saida_historico h
          ON h.id_saida = sa2.id_saida
         AND h.evento = 'base_transferida'
        WHERE sa2.id_coleta IS NOT NULL
    ) t ON t.id_coleta = sa.id_coleta
    GROUP BY sa.id_coleta, sa.sub_base
) s
  ON s.id_coleta = c.id_coleta
 AND s.sub_base IS NOT DISTINCT FROM c.sub_base
WHERE s.dias = 1
  AND c.timestamp::date IS DISTINCT FROM s.data_op
ORDER BY c.sub_base, c.base, s.data_op;

-- 2) Correção
UPDATE coletas c
SET timestamp = s.data_op::timestamp
FROM (
    SELECT
        sa.id_coleta,
        sa.sub_base,
        MIN(sa.data) AS data_op,
        COUNT(DISTINCT sa.data) AS dias
    FROM saidas sa
    JOIN (
        SELECT DISTINCT sa2.id_coleta
        FROM saidas sa2
        JOIN saida_historico h
          ON h.id_saida = sa2.id_saida
         AND h.evento = 'base_transferida'
        WHERE sa2.id_coleta IS NOT NULL
    ) t ON t.id_coleta = sa.id_coleta
    GROUP BY sa.id_coleta, sa.sub_base
) s
WHERE c.id_coleta = s.id_coleta
  AND s.sub_base IS NOT DISTINCT FROM c.sub_base
  AND s.dias = 1
  AND c.timestamp::date IS DISTINCT FROM s.data_op;

-- 3) Casos não alterados: a mesma coleta tem pacotes de dias diferentes
SELECT
    c.sub_base,
    c.id_coleta,
    c.base,
    c.timestamp AS data_gravada,
    s.dias,
    s.qtd_saidas
FROM coletas c
JOIN (
    SELECT
        sa.id_coleta,
        sa.sub_base,
        COUNT(DISTINCT sa.data) AS dias,
        COUNT(*) AS qtd_saidas
    FROM saidas sa
    JOIN (
        SELECT DISTINCT sa2.id_coleta
        FROM saidas sa2
        JOIN saida_historico h
          ON h.id_saida = sa2.id_saida
         AND h.evento = 'base_transferida'
        WHERE sa2.id_coleta IS NOT NULL
    ) t ON t.id_coleta = sa.id_coleta
    GROUP BY sa.id_coleta, sa.sub_base
) s
  ON s.id_coleta = c.id_coleta
 AND s.sub_base IS NOT DISTINCT FROM c.sub_base
WHERE s.dias <> 1
ORDER BY c.sub_base, c.id_coleta;
