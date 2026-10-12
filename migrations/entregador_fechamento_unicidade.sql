-- Fechamento de Motoboys v2: impede dois fechamentos do mesmo executor no mesmo período.
-- A constraint uq_entregador_fechamento_periodo não protege porque id_entregador
-- ou id_motoboy é sempre NULL. Aditiva e idempotente; não apaga nada.
-- Se houver duplicatas, a migration falha listando-as para correção manual.

BEGIN;

DO $$
DECLARE
    dup RECORD;
    qtd INTEGER := 0;
BEGIN
    FOR dup IN
        SELECT sub_base, 'motoboy' AS tipo, id_motoboy AS executor_id, periodo_inicio, periodo_fim,
               array_agg(id_fechamento ORDER BY id_fechamento) AS ids
          FROM entregador_fechamentos
         WHERE id_motoboy IS NOT NULL
         GROUP BY sub_base, id_motoboy, periodo_inicio, periodo_fim
        HAVING count(*) > 1
        UNION ALL
        SELECT sub_base, 'entregador', id_entregador, periodo_inicio, periodo_fim,
               array_agg(id_fechamento ORDER BY id_fechamento)
          FROM entregador_fechamentos
         WHERE id_entregador IS NOT NULL
         GROUP BY sub_base, id_entregador, periodo_inicio, periodo_fim
        HAVING count(*) > 1
    LOOP
        qtd := qtd + 1;
        RAISE WARNING 'Fechamento duplicado: sub_base=% % % periodo=%..% ids=%',
            dup.sub_base, dup.tipo, dup.executor_id, dup.periodo_inicio, dup.periodo_fim, dup.ids;
    END LOOP;
    IF qtd > 0 THEN
        RAISE EXCEPTION 'Existem % grupos de fechamentos duplicados. Corrija antes de criar os índices.', qtd;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_entregador_fechamento_motoboy_periodo
    ON entregador_fechamentos (sub_base, id_motoboy, periodo_inicio, periodo_fim)
    WHERE id_motoboy IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_entregador_fechamento_entregador_periodo
    ON entregador_fechamentos (sub_base, id_entregador, periodo_inicio, periodo_fim)
    WHERE id_entregador IS NOT NULL;

COMMIT;
