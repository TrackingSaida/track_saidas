-- Auditoria e índice para busca exata por código na tela Registros (GET /saidas/listar).
-- Executar manualmente em janela de manutenção (CREATE INDEX CONCURRENTLY).

-- 1) Verificar duplicados (sub_base, codigo) antes de considerar índice UNIQUE
SELECT sub_base, codigo, COUNT(*)
FROM saidas
WHERE codigo IS NOT NULL
GROUP BY sub_base, codigo
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC
LIMIT 50;

-- 2) Índice composto para lookup tenant-safe (sub_base + codigo)
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_saidas_sub_base_codigo
  ON saidas (sub_base, codigo)
  WHERE codigo IS NOT NULL;

-- 3) Confirmar índice criado
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'saidas'
  AND indexname = 'ix_saidas_sub_base_codigo';
