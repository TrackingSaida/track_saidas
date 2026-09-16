-- Políticas gerais: defaults Motoboy no owner + claims_version + idle session
-- Idempotente. Não altera ignorar_coleta / entrada_obrigatoria_habilitada.

-- Defaults Motoboy por base (Owner)
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS default_pode_realizar_coleta BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS default_pode_ler_saida BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS default_pode_digitar_codigo_manual BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS default_pode_lancar_avulso BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS default_avulso_exige_foto BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN owner.default_pode_realizar_coleta IS
  'Padrão para novos motoboys: pode realizar coleta (default false).';
COMMENT ON COLUMN owner.default_pode_ler_saida IS
  'Padrão para novos motoboys: pode ler saída (default true).';
COMMENT ON COLUMN owner.default_pode_digitar_codigo_manual IS
  'Padrão para novos motoboys: digitar código manual (default false).';
COMMENT ON COLUMN owner.default_pode_lancar_avulso IS
  'Padrão para novos motoboys: lançar avulso (default true).';
COMMENT ON COLUMN owner.default_avulso_exige_foto IS
  'Padrão para novos motoboys: avulso exige foto (default true).';

-- Com coleta ativa: bloquear saída sem coleta (false = avisar e permitir; legado)
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS bloquear_saida_sem_coleta BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN owner.bloquear_saida_sem_coleta IS
  'Com coleta ativa: true impede registrar saída sem coleta; false avisa e permite.';

-- Versão de claims do motoboy (bump ao mudar permissões)
ALTER TABLE motoboys
ADD COLUMN IF NOT EXISTS claims_version INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN motoboys.claims_version IS
  'Incrementado ao alterar permissões; JWT stale → refresh silencioso.';

-- Sessão deslizante: última atividade do refresh
ALTER TABLE motoboy_refresh_tokens
ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;

UPDATE motoboy_refresh_tokens
SET last_activity_at = COALESCE(created_at, NOW())
WHERE last_activity_at IS NULL;

ALTER TABLE motoboy_refresh_tokens
ALTER COLUMN last_activity_at SET DEFAULT NOW();

ALTER TABLE motoboy_refresh_tokens
ALTER COLUMN last_activity_at SET NOT NULL;

COMMENT ON COLUMN motoboy_refresh_tokens.last_activity_at IS
  'Última atividade da sessão; idle timeout usa este campo.';
