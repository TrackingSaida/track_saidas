-- Backfill de cobrança owner para saídas do fluxo /mobile/scan sem item ativo.
-- Regras:
-- 1) só inclui saídas que possuem evento "scan" no histórico;
-- 2) não inclui saídas canceladas;
-- 3) não duplica cobrança quando já existe item ativo (cancelado = false).

BEGIN;

-- Índice de apoio para checagem idempotente por saída (seguro reaplicar).
CREATE INDEX IF NOT EXISTS idx_owner_cobranca_itens_saida_cancelado
    ON owner_cobranca_itens (id_saida, cancelado);

WITH saidas_scan AS (
    SELECT DISTINCT s.id_saida, s.sub_base
    FROM saidas s
    JOIN saida_historico h
      ON h.id_saida = s.id_saida
     AND h.evento = 'scan'
    WHERE s.sub_base IS NOT NULL
      AND COALESCE(LOWER(s.status), '') NOT IN ('cancelado', 'cancelada')
),
faltantes AS (
    SELECT ss.id_saida, ss.sub_base
    FROM saidas_scan ss
    WHERE NOT EXISTS (
        SELECT 1
        FROM owner_cobranca_itens oci
        WHERE oci.id_saida = ss.id_saida
          AND COALESCE(oci.cancelado, FALSE) = FALSE
    )
)
INSERT INTO owner_cobranca_itens (sub_base, id_coleta, id_saida, valor)
SELECT
    f.sub_base,
    NULL AS id_coleta,
    f.id_saida,
    COALESCE(o.valor, 0) AS valor
FROM faltantes f
LEFT JOIN owner o
       ON o.sub_base = f.sub_base;

COMMIT;
