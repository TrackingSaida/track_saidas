-- Migração: EntregadorFechamento suporta executor entregador OU motoboy
-- Executar manualmente no banco antes de subir a nova versão.
-- Exatamente um de id_entregador ou id_motoboy deve ser preenchido (validado na aplicação).
--
-- Pré-requisito: a tabela motoboys deve existir (ex.: migrations/motoboy_perfil.sql já aplicada).
--
-- Exemplo de execução (substitua CONNECTION_STRING pela URL do PostgreSQL, ex. do Render):
--   psql "CONNECTION_STRING" -f migrations/entregador_fechamento_id_motoboy.sql
--

-- 1. Adicionar coluna id_motoboy (nullable, FK motoboys)
ALTER TABLE entregador_fechamentos
ADD COLUMN IF NOT EXISTS id_motoboy BIGINT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE;

-- 2. Tornar id_entregador nullable
ALTER TABLE entregador_fechamentos
ALTER COLUMN id_entregador DROP NOT NULL;

-- 3. Tornar username_entregador nullable (motoboy pode usar username do User)
ALTER TABLE entregador_fechamentos
ALTER COLUMN username_entregador DROP NOT NULL;

-- 4. Remover constraint única antiga e criar nova (inclui id_motoboy)
ALTER TABLE entregador_fechamentos
DROP CONSTRAINT IF EXISTS uq_entregador_fechamento_periodo;

ALTER TABLE entregador_fechamentos
ADD CONSTRAINT uq_entregador_fechamento_periodo UNIQUE (
    sub_base, id_entregador, id_motoboy, periodo_inicio, periodo_fim
);
