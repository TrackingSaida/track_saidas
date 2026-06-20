-- Continuidade de rota: sub_base, updated_at, índice UNIQUE parcial (idempotente)

ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS sub_base TEXT;
ALTER TABLE rotas_motoboy ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Backfill sub_base a partir do motoboy (executar após ADD COLUMN)
UPDATE rotas_motoboy r
SET sub_base = msb.sub_base
FROM motoboy_sub_base msb
WHERE r.sub_base IS NULL
  AND msb.motoboy_id = r.motoboy_id
  AND msb.ativo IS TRUE
  AND msb.sub_base IS NOT NULL;

-- Fallback: sub_base do user vinculado ao motoboy
UPDATE rotas_motoboy r
SET sub_base = u.sub_base
FROM motoboys m
JOIN users u ON u.id = m.user_id
WHERE r.sub_base IS NULL
  AND m.id_motoboy = r.motoboy_id
  AND u.sub_base IS NOT NULL;

-- Cancela rotas abertas duplicadas antes do índice UNIQUE (idempotente).
-- Mantém 1 por (sub_base, motoboy_id, data): ativa > preparando > mais recente.
WITH abertas AS (
    SELECT
        r.id,
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_rotas_motoboy_subbase_dia_aberta
    ON rotas_motoboy (sub_base, motoboy_id, data)
    WHERE status IN ('preparando', 'ativa') AND finalizado_em IS NULL;

CREATE INDEX IF NOT EXISTS ix_rotas_motoboy_subbase_status_data
    ON rotas_motoboy (sub_base, motoboy_id, data, status);
