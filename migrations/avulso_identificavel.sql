-- Avulso Identificável: config EAV + lote + flags em saidas
-- Idempotente. Não altera fluxo financeiro.

CREATE TABLE IF NOT EXISTS avulso_lote (
  id BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  origem TEXT NOT NULL, -- coleta | entrada | saida_excecao | saida
  quantidade INTEGER NOT NULL DEFAULT 1,
  criado_por BIGINT NULL,
  motivo_excepcional TEXT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_avulso_lote_sub_base_created
  ON avulso_lote (sub_base, created_at DESC);

COMMENT ON TABLE avulso_lote IS
  'Agrupa N saídas Avulso criadas na mesma ação operacional.';

CREATE TABLE IF NOT EXISTS avulso_campo_config (
  id BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  contexto TEXT NOT NULL DEFAULT 'TODOS_AVULSO',
  -- COLETA_AVULSO | ENTRADA_AVULSO | SAIDA_AVULSO | TODOS_AVULSO
  chave TEXT NOT NULL,
  label TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'texto', -- texto | telefone | numero | foto | lista
  obrigatorio BOOLEAN NOT NULL DEFAULT false,
  usar_na_identificacao BOOLEAN NOT NULL DEFAULT false,
  exibir_na_selecao BOOLEAN NOT NULL DEFAULT true,
  ordem INTEGER NOT NULL DEFAULT 0,
  ativo BOOLEAN NOT NULL DEFAULT true,
  opcoes_json TEXT NULL, -- JSON array para tipo lista
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_avulso_campo_config_sub_ctx_chave UNIQUE (sub_base, contexto, chave)
);

CREATE INDEX IF NOT EXISTS ix_avulso_campo_config_sub_ativo
  ON avulso_campo_config (sub_base, ativo, ordem);

COMMENT ON TABLE avulso_campo_config IS
  'Campos dinâmicos de identificação de Avulso por sub_base/contexto.';

CREATE TABLE IF NOT EXISTS avulso_campo_valor (
  id BIGSERIAL PRIMARY KEY,
  id_saida BIGINT NOT NULL,
  campo_config_id BIGINT NOT NULL REFERENCES avulso_campo_config(id) ON DELETE CASCADE,
  valor_texto TEXT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_avulso_campo_valor_saida_campo UNIQUE (id_saida, campo_config_id)
);

CREATE INDEX IF NOT EXISTS ix_avulso_campo_valor_saida
  ON avulso_campo_valor (id_saida);

CREATE INDEX IF NOT EXISTS ix_avulso_campo_valor_busca
  ON avulso_campo_valor (campo_config_id, valor_texto);

COMMENT ON TABLE avulso_campo_valor IS
  'Valores EAV dos campos dinâmicos de Avulso por id_saida.';

ALTER TABLE saidas
ADD COLUMN IF NOT EXISTS avulso_lote_id BIGINT NULL;

ALTER TABLE saidas
ADD COLUMN IF NOT EXISTS avulso_criado_excepcional BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS ix_saidas_avulso_lote_id
  ON saidas (avulso_lote_id)
  WHERE avulso_lote_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_saidas_avulso_pendentes
  ON saidas (sub_base, status)
  WHERE lower(coalesce(servico, '')) LIKE '%avulso%';

COMMENT ON COLUMN saidas.avulso_lote_id IS
  'Lote de criação em massa de avulsos (nullable).';
COMMENT ON COLUMN saidas.avulso_criado_excepcional IS
  'True quando criado na saída fora do fluxo normal (exige motivo).';
