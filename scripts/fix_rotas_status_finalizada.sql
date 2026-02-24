-- Corrige rotas que têm finalizado_em preenchido mas status ainda 'ativa'.
-- Rodar uma vez no banco para alinhar dados (ex.: após ajuste manual de finalizado_em).
-- Depois disso, GET /mobile/rotas/ativa já não retorna essas rotas (filtro finalizado_em IS NULL).

UPDATE rotas_motoboy
SET status = 'finalizada'
WHERE finalizado_em IS NOT NULL
  AND (status IS NULL OR status = 'ativa');
