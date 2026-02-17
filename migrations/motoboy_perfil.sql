-- Migração: Perfil Motoboy (role=4)
-- Executar manualmente no banco antes de subir a nova versão

-- 1.1 Alterar tabela motoboys (adicionar permissões)
ALTER TABLE motoboys
ADD COLUMN IF NOT EXISTS pode_ler_coleta BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN IF NOT EXISTS pode_ler_saida BOOLEAN NOT NULL DEFAULT true;

-- 1.2 Criar tabela motoboy_sub_base
CREATE TABLE IF NOT EXISTS motoboy_sub_base (
    id BIGSERIAL PRIMARY KEY,
    motoboy_id BIGINT NOT NULL REFERENCES motoboys(id_motoboy) ON DELETE CASCADE,
    sub_base TEXT NOT NULL,
    ativo BOOLEAN NOT NULL DEFAULT true
);
