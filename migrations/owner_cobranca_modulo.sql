-- Cobrança de Owners (Financeiro, somente root).
-- Aditiva e idempotente. Não altera owner_cobranca_itens nem tabelas operacionais.
-- Rollback: migrations/owner_cobranca_modulo_rollback.sql (remove só as tabelas novas).

BEGIN;

-- ---------------------------------------------------------------
-- Configuração comercial por Owner
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_configuracao_cobranca (
  id                  BIGSERIAL PRIMARY KEY,
  owner_id            BIGINT NOT NULL REFERENCES owner(id_owner) ON DELETE CASCADE,
  sub_base            TEXT NOT NULL,
  modelo              TEXT NOT NULL,
  modelo_sugerido     TEXT NULL,
  periodicidade       TEXT NOT NULL DEFAULT 'quinzenal',
  status              TEXT NOT NULL DEFAULT 'pendente_confirmacao',
  razao_social        TEXT NULL,
  documento           TEXT NULL,
  telefone            TEXT NULL,
  endereco            TEXT NULL,
  contato_financeiro  TEXT NULL,
  observacao          TEXT NULL,
  confirmado_por      BIGINT NULL,
  confirmado_em       TIMESTAMP NULL,
  atualizado_por      BIGINT NULL,
  criado_em           TIMESTAMP NOT NULL DEFAULT now(),
  atualizado_em       TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_owner_config_cobranca_owner UNIQUE (owner_id),
  CONSTRAINT ck_owner_config_cobranca_modelo CHECK (
    modelo IN ('coleta_saida', 'entrada_saida', 'coleta_entrada_saida', 'somente_saida')
  ),
  CONSTRAINT ck_owner_config_cobranca_periodicidade CHECK (periodicidade IN ('quinzenal', 'mensal')),
  CONSTRAINT ck_owner_config_cobranca_status CHECK (status IN ('pendente_confirmacao', 'confirmado'))
);

-- ---------------------------------------------------------------
-- Preço por vigência (o preço do período é o vigente em periodo_inicio)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_preco_vigencia (
  id           BIGSERIAL PRIMARY KEY,
  owner_id     BIGINT NOT NULL REFERENCES owner(id_owner) ON DELETE CASCADE,
  valor        NUMERIC(12, 4) NOT NULL,
  inicio       DATE NOT NULL,
  fim          DATE NULL,
  motivo       TEXT NULL,
  criado_por   BIGINT NULL,
  criado_em    TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_owner_preco_vigencia_inicio UNIQUE (owner_id, inicio),
  CONSTRAINT ck_owner_preco_vigencia_valor CHECK (valor >= 0),
  CONSTRAINT ck_owner_preco_vigencia_fim CHECK (fim IS NULL OR fim >= inicio)
);

-- ---------------------------------------------------------------
-- Aceite "Conferido" de período aberto (não guarda quantidade)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_apuracao (
  id                       BIGSERIAL PRIMARY KEY,
  owner_id                 BIGINT NOT NULL REFERENCES owner(id_owner) ON DELETE CASCADE,
  sub_base                 TEXT NOT NULL,
  periodo_inicio           DATE NOT NULL,
  periodo_fim              DATE NOT NULL,
  hash                     TEXT NOT NULL,
  conferido_por            BIGINT NULL,
  conferido_por_username   TEXT NULL,
  conferido_em             TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_owner_apuracao_periodo UNIQUE (owner_id, periodo_inicio, periodo_fim)
);

-- ---------------------------------------------------------------
-- Decisões administrativas sobre coleta manual ambígua
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_apuracao_decisao (
  id                     BIGSERIAL PRIMARY KEY,
  owner_id               BIGINT NOT NULL REFERENCES owner(id_owner) ON DELETE CASCADE,
  sub_base               TEXT NOT NULL,
  periodo_inicio         DATE NOT NULL,
  periodo_fim            DATE NOT NULL,
  tipo                   TEXT NOT NULL,
  id_coleta              BIGINT NOT NULL,
  servico                TEXT NOT NULL,
  codigos                JSONB NULL,
  quantidade_ajustada    INTEGER NULL,
  motivo                 TEXT NOT NULL,
  ativo                  BOOLEAN NOT NULL DEFAULT true,
  criado_por             BIGINT NULL,
  criado_por_username    TEXT NULL,
  criado_em              TIMESTAMP NOT NULL DEFAULT now(),
  revogado_por           BIGINT NULL,
  revogado_em            TIMESTAMP NULL,
  CONSTRAINT ck_owner_apuracao_decisao_tipo CHECK (
    tipo IN ('volumes_distintos', 'vinculo_codigos', 'ajuste_quantidade')
  ),
  CONSTRAINT ck_owner_apuracao_decisao_qtd CHECK (quantidade_ajustada IS NULL OR quantidade_ajustada >= 0)
);
CREATE INDEX IF NOT EXISTS ix_owner_apuracao_decisao_periodo
  ON owner_apuracao_decisao (owner_id, periodo_inicio, periodo_fim)
  WHERE ativo;

-- ---------------------------------------------------------------
-- Fechamento (snapshot imutável, versionado)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS owner_fechamento (
  id                     BIGSERIAL PRIMARY KEY,
  owner_id               BIGINT NOT NULL REFERENCES owner(id_owner) ON DELETE RESTRICT,
  sub_base               TEXT NOT NULL,
  periodo_inicio         DATE NOT NULL,
  periodo_fim            DATE NOT NULL,
  versao                 INTEGER NOT NULL DEFAULT 1,
  status                 TEXT NOT NULL DEFAULT 'fechado',
  modelo                 TEXT NOT NULL,
  periodicidade          TEXT NOT NULL,
  preco_unitario         NUMERIC(12, 4) NOT NULL,
  qtd_bruto              INTEGER NOT NULL DEFAULT 0,
  qtd_cancelados         INTEGER NOT NULL DEFAULT 0,
  qtd_faturavel          INTEGER NOT NULL DEFAULT 0,
  qtd_desconsiderados    INTEGER NOT NULL DEFAULT 0,
  valor_pacotes          NUMERIC(14, 2) NOT NULL DEFAULT 0,
  valor_ajustes          NUMERIC(14, 2) NOT NULL DEFAULT 0,
  valor_total            NUMERIC(14, 2) NOT NULL DEFAULT 0,
  hash                   TEXT NOT NULL,
  owner_snapshot         JSONB NOT NULL DEFAULT '{}'::jsonb,
  config_snapshot        JSONB NOT NULL DEFAULT '{}'::jsonb,
  observacao             TEXT NULL,
  fechado_por            BIGINT NULL,
  fechado_por_username   TEXT NULL,
  fechado_em             TIMESTAMP NOT NULL DEFAULT now(),
  informado_por          BIGINT NULL,
  informado_em           TIMESTAMP NULL,
  reaberto_por           BIGINT NULL,
  reaberto_em            TIMESTAMP NULL,
  reaberto_motivo        TEXT NULL,
  anulado_por            BIGINT NULL,
  anulado_em             TIMESTAMP NULL,
  anulado_motivo         TEXT NULL,
  CONSTRAINT uq_owner_fechamento_versao UNIQUE (owner_id, periodo_inicio, periodo_fim, versao),
  CONSTRAINT ck_owner_fechamento_status CHECK (status IN ('fechado', 'informado', 'reaberto', 'anulado')),
  CONSTRAINT ck_owner_fechamento_periodo CHECK (periodo_fim >= periodo_inicio)
);
-- Só uma versão vigente por Owner e período.
CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_fechamento_vigente
  ON owner_fechamento (owner_id, periodo_inicio, periodo_fim)
  WHERE status IN ('fechado', 'informado');
CREATE INDEX IF NOT EXISTS ix_owner_fechamento_periodo
  ON owner_fechamento (periodo_inicio, periodo_fim);

CREATE TABLE IF NOT EXISTS owner_fechamento_composicao (
  id              BIGSERIAL PRIMARY KEY,
  fechamento_id   BIGINT NOT NULL REFERENCES owner_fechamento(id) ON DELETE CASCADE,
  classificacao   TEXT NOT NULL,
  quantidade      INTEGER NOT NULL DEFAULT 0,
  valor           NUMERIC(14, 2) NOT NULL DEFAULT 0,
  ordem           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_owner_fechamento_composicao_fech
  ON owner_fechamento_composicao (fechamento_id);

-- Item com código real ou referência ao lançamento manual (sem código fictício).
CREATE TABLE IF NOT EXISTS owner_fechamento_item (
  id                  BIGSERIAL PRIMARY KEY,
  fechamento_id       BIGINT NOT NULL REFERENCES owner_fechamento(id) ON DELETE CASCADE,
  tipo                TEXT NOT NULL,
  codigo              TEXT NULL,
  id_saida            BIGINT NULL,
  id_coleta           BIGINT NULL,
  avulso_lote_id      BIGINT NULL,
  servico             TEXT NULL,
  base                TEXT NULL,
  data_competencia    DATE NULL,
  origem              TEXT NOT NULL,
  status_financeiro   TEXT NOT NULL,
  motivo              TEXT NULL,
  quantidade          INTEGER NOT NULL DEFAULT 1,
  is_grande           BOOLEAN NOT NULL DEFAULT false,
  status_operacional  TEXT NULL,
  CONSTRAINT ck_owner_fechamento_item_tipo CHECK (tipo IN ('codigo', 'manual'))
);
CREATE INDEX IF NOT EXISTS ix_owner_fechamento_item_fech
  ON owner_fechamento_item (fechamento_id, id);

-- Ajuste em dinheiro: nasce de um fechamento e entra no próximo período fechado.
CREATE TABLE IF NOT EXISTS owner_fechamento_ajuste (
  id                       BIGSERIAL PRIMARY KEY,
  owner_id                 BIGINT NOT NULL REFERENCES owner(id_owner) ON DELETE RESTRICT,
  sub_base                 TEXT NOT NULL,
  fechamento_origem_id     BIGINT NULL REFERENCES owner_fechamento(id) ON DELETE SET NULL,
  fechamento_aplicado_id   BIGINT NULL REFERENCES owner_fechamento(id) ON DELETE SET NULL,
  tipo                     TEXT NOT NULL,
  valor                    NUMERIC(14, 2) NOT NULL,
  motivo                   TEXT NOT NULL,
  chave_idempotencia       TEXT NOT NULL,
  criado_por               BIGINT NULL,
  criado_por_username      TEXT NULL,
  criado_em                TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_owner_fechamento_ajuste_chave UNIQUE (chave_idempotencia),
  CONSTRAINT ck_owner_fechamento_ajuste_tipo CHECK (tipo IN ('acrescimo', 'desconto', 'credito', 'correcao'))
);
CREATE INDEX IF NOT EXISTS ix_owner_fechamento_ajuste_pendente
  ON owner_fechamento_ajuste (owner_id)
  WHERE fechamento_aplicado_id IS NULL;

CREATE TABLE IF NOT EXISTS owner_fechamento_historico (
  id                BIGSERIAL PRIMARY KEY,
  owner_id          BIGINT NOT NULL,
  periodo_inicio    DATE NOT NULL,
  periodo_fim       DATE NOT NULL,
  fechamento_id     BIGINT NULL REFERENCES owner_fechamento(id) ON DELETE SET NULL,
  acao              TEXT NOT NULL,
  status_anterior   TEXT NULL,
  status_novo       TEXT NULL,
  motivo            TEXT NULL,
  detalhes          JSONB NULL,
  user_id           BIGINT NULL,
  username          TEXT NULL,
  criado_em         TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_owner_fechamento_historico_periodo
  ON owner_fechamento_historico (owner_id, periodo_inicio, periodo_fim, criado_em);

-- Impede cobrar o mesmo código em dois fechamentos vigentes.
CREATE TABLE IF NOT EXISTS owner_unidade_faturada (
  id              BIGSERIAL PRIMARY KEY,
  fechamento_id   BIGINT NOT NULL REFERENCES owner_fechamento(id) ON DELETE CASCADE,
  owner_id        BIGINT NOT NULL,
  sub_base        TEXT NOT NULL,
  codigo          TEXT NOT NULL,
  ativo           BOOLEAN NOT NULL DEFAULT true,
  criado_em       TIMESTAMP NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_owner_unidade_faturada_ativa
  ON owner_unidade_faturada (sub_base, codigo)
  WHERE ativo;
CREATE INDEX IF NOT EXISTS ix_owner_unidade_faturada_fech
  ON owner_unidade_faturada (fechamento_id);

-- ---------------------------------------------------------------
-- Seed: configuração sugerida (pendente) e preço inicial copiado de owner.valor.
-- Nada é confirmado nem fechado automaticamente.
-- ---------------------------------------------------------------
INSERT INTO owner_configuracao_cobranca (owner_id, sub_base, modelo, modelo_sugerido, periodicidade, status)
SELECT o.id_owner,
       COALESCE(o.sub_base, ''),
       s.modelo,
       s.modelo,
       'quinzenal',
       'pendente_confirmacao'
FROM owner o
CROSS JOIN LATERAL (
  SELECT CASE
    WHEN COALESCE(o.entrada_obrigatoria_habilitada, false) AND NOT COALESCE(o.ignorar_coleta, false)
         AND COALESCE(o.modo_operacao, 'codigo') <> 'coleta_manual'
      THEN 'coleta_entrada_saida'
    WHEN COALESCE(o.entrada_obrigatoria_habilitada, false)
      THEN 'entrada_saida'
    WHEN COALESCE(o.ignorar_coleta, false)
      THEN 'somente_saida'
    ELSE 'coleta_saida'
  END AS modelo
) s
WHERE o.sub_base IS NOT NULL
ON CONFLICT (owner_id) DO NOTHING;

INSERT INTO owner_preco_vigencia (owner_id, valor, inicio, motivo)
SELECT o.id_owner, COALESCE(o.valor, 0), DATE '2000-01-01', 'Preço inicial copiado do cadastro do Owner'
FROM owner o
WHERE o.sub_base IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM owner_preco_vigencia v WHERE v.owner_id = o.id_owner);

COMMIT;
