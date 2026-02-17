-- Migração: Fluxo App Motoboy (motoboy_id, data_hora_entrega, motivo_ausencia, saida_historico)
-- Executar manualmente no banco antes de subir a nova versão

-- 1. Saidas: motoboy_id e data_hora_entrega
ALTER TABLE saidas
ADD COLUMN IF NOT EXISTS motoboy_id BIGINT REFERENCES motoboys(id_motoboy) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS data_hora_entrega TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_saidas_motoboy_id ON saidas(motoboy_id);

-- 2. Tabela motivo_ausencia
CREATE TABLE IF NOT EXISTS motivo_ausencia (
    id BIGSERIAL PRIMARY KEY,
    descricao TEXT NOT NULL,
    ativo BOOLEAN NOT NULL DEFAULT true
);

-- Seed motivos iniciais (idempotente)
INSERT INTO motivo_ausencia (descricao, ativo)
SELECT 'Cliente ausente', true WHERE NOT EXISTS (SELECT 1 FROM motivo_ausencia WHERE descricao = 'Cliente ausente');
INSERT INTO motivo_ausencia (descricao, ativo)
SELECT 'Endereço não encontrado', true WHERE NOT EXISTS (SELECT 1 FROM motivo_ausencia WHERE descricao = 'Endereço não encontrado');
INSERT INTO motivo_ausencia (descricao, ativo)
SELECT 'Recusado', true WHERE NOT EXISTS (SELECT 1 FROM motivo_ausencia WHERE descricao = 'Recusado');
INSERT INTO motivo_ausencia (descricao, ativo)
SELECT 'Outro', true WHERE NOT EXISTS (SELECT 1 FROM motivo_ausencia WHERE descricao = 'Outro');

-- 3. Tabela saida_historico
CREATE TABLE IF NOT EXISTS saida_historico (
    id BIGSERIAL PRIMARY KEY,
    id_saida BIGINT NOT NULL,
    evento TEXT NOT NULL,
    motoboy_id_anterior BIGINT,
    motoboy_id_novo BIGINT,
    user_id BIGINT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS ix_saida_historico_id_saida ON saida_historico(id_saida);
