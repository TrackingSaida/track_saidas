-- Atualizar latitude/longitude em saidas_detail
--
-- IMPORTANTE: Na tabela saidas_detail:
--   id_detail = PK da linha (1, 2, 3, 4, 5...)
--   id_saida  = ID da entrega (ex: 110967, 111965, 110976...)  <-- use este no WHERE
-- Não use id_detail no WHERE; use o id_saida que aparece na consulta abaixo.
--
-- 1) Rode esta consulta e anote o id_saida de cada linha (não confunda com id_detail):
SELECT id_detail, id_saida, endereco_formatado, latitude, longitude
FROM saidas_detail
WHERE (endereco_formatado IS NOT NULL AND TRIM(endereco_formatado) != '')
  AND (latitude IS NULL OR longitude IS NULL)
ORDER BY id_saida;

-- 2) Substitua os ??? pelos id_saida que apareceram na consulta (números grandes, ex: 111965) e execute:

-- Par 1: -23.48677617994254, -46.86980660084137
UPDATE saidas_detail
SET latitude = -23.48677617994254, longitude = -46.86980660084137
WHERE id_saida = ???;

-- Par 2: -23.540880239826883, -46.82670901656605
UPDATE saidas_detail
SET latitude = -23.540880239826883, longitude = -46.82670901656605
WHERE id_saida = ???;
