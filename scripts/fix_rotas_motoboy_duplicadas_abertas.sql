-- Corrige rotas abertas duplicadas (preparando/ativa, finalizado_em NULL)
-- antes de criar uq_rotas_motoboy_subbase_dia_aberta.
--
-- Regra: mantém 1 rota por (sub_base, motoboy_id, data).
-- Prioridade: status 'ativa' > 'preparando'; depois maior iniciado_em/updated_at/id.

-- 1) Auditoria (opcional)
-- SELECT sub_base, motoboy_id, data, COUNT(*) AS qtd, array_agg(id ORDER BY id) AS ids
-- FROM rotas_motoboy
-- WHERE status IN ('preparando', 'ativa')
--   AND finalizado_em IS NULL
--   AND sub_base IS NOT NULL
-- GROUP BY sub_base, motoboy_id, data
-- HAVING COUNT(*) > 1
-- ORDER BY qtd DESC;

-- 2) Cancela duplicatas, preservando a rota vencedora por grupo
WITH abertas AS (
    SELECT
        r.id,
        r.sub_base,
        r.motoboy_id,
        r.data,
        ROW_NUMBER() OVER (
            PARTITION BY r.sub_base, r.motoboy_id, r.data
            ORDER BY
                CASE r.status WHEN 'ativa' THEN 0 WHEN 'preparando' THEN 1 ELSE 2 END,
                COALESCE(r.updated_at, r.iniciado_em, TIMESTAMP '1970-01-01') DESC,
                r.id DESC
        ) AS rn
    FROM rotas_motoboy r
    WHERE r.status IN ('preparando', 'ativa')
      AND r.finalizado_em IS NULL
      AND r.sub_base IS NOT NULL
)
UPDATE rotas_motoboy r
SET
    status = 'cancelada',
    finalizado_em = COALESCE(r.finalizado_em, NOW()),
    updated_at = NOW()
FROM abertas a
WHERE r.id = a.id
  AND a.rn > 1;

-- 3) Conferir se ainda há duplicatas
-- SELECT sub_base, motoboy_id, data, COUNT(*)
-- FROM rotas_motoboy
-- WHERE status IN ('preparando', 'ativa') AND finalizado_em IS NULL AND sub_base IS NOT NULL
-- GROUP BY sub_base, motoboy_id, data
-- HAVING COUNT(*) > 1;
