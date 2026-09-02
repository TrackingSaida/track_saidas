-- ML Int: impede duplicata de seller por sub_base e limpa duplicatas existentes.
-- Executar uma vez no Postgres de produção/homologação.

-- 1) Mantém a conexão mais recente por (user_id_ml, sub_base) e remove as demais.
WITH ranked AS (
  SELECT
    id,
    ROW_NUMBER() OVER (
      PARTITION BY user_id_ml, sub_base
      ORDER BY criado_em DESC NULLS LAST, id DESC
    ) AS rn
  FROM ml_conexoes
)
DELETE FROM ml_conexoes
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- 2) Constraint única para impedir novas duplicatas.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'uq_ml_conexoes_user_sub_base'
  ) THEN
    ALTER TABLE ml_conexoes
      ADD CONSTRAINT uq_ml_conexoes_user_sub_base UNIQUE (user_id_ml, sub_base);
  END IF;
END $$;
