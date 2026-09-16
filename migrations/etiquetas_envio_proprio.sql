-- Etiquetas envio próprio: identidade visual do Owner + snapshot + sequence RTE
-- Idempotente. Não altera fluxos financeiros existentes.

-- ============================================================
-- Owner: logo + slogan
-- ============================================================
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS logo_object_key TEXT NULL;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS logo_filename TEXT NULL;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS logo_content_type TEXT NULL;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS logo_updated_at TIMESTAMP WITHOUT TIME ZONE NULL;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS slogan TEXT NULL;

COMMENT ON COLUMN owner.logo_object_key IS
  'Object key B2 da logo da Base (owner/{id}/logo/...). NULL = fallback ROTEVO.';
COMMENT ON COLUMN owner.slogan IS
  'Texto curto exibido no header da etiqueta de envio próprio.';

-- ============================================================
-- Sequence global para códigos RTE
-- ============================================================
CREATE SEQUENCE IF NOT EXISTS envio_proprio_codigo_seq
  AS BIGINT
  START WITH 1
  INCREMENT BY 1
  NO MINVALUE
  NO MAXVALUE
  CACHE 1;

-- ============================================================
-- Snapshot imutável da etiqueta (código globalmente único)
-- ============================================================
CREATE TABLE IF NOT EXISTS envio_proprio (
  id_envio BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  owner_id BIGINT NULL REFERENCES owner(id_owner) ON DELETE SET NULL,
  id_saida BIGINT NULL,
  codigo TEXT NOT NULL,
  origem_remetente TEXT NOT NULL, -- seller | manual
  id_base BIGINT NULL,

  remetente_nome TEXT NOT NULL,
  remetente_telefone TEXT NULL,
  remetente_cep TEXT NOT NULL,
  remetente_rua TEXT NOT NULL,
  remetente_numero TEXT NOT NULL,
  remetente_complemento TEXT NULL,
  remetente_bairro TEXT NOT NULL,
  remetente_cidade TEXT NOT NULL,
  remetente_uf TEXT NOT NULL,

  dest_nome TEXT NOT NULL,
  dest_telefone TEXT NULL,
  dest_cep TEXT NOT NULL,
  dest_rua TEXT NOT NULL,
  dest_numero TEXT NOT NULL,
  dest_complemento TEXT NULL,
  dest_bairro TEXT NOT NULL,
  dest_cidade TEXT NOT NULL,
  dest_uf TEXT NOT NULL,

  peso_kg NUMERIC(10, 3) NULL,
  dimensoes TEXT NULL,
  observacao TEXT NULL,

  owner_nome_exibicao TEXT NULL,
  owner_slogan TEXT NULL,
  logo_object_key_used TEXT NULL,

  criado_por_user_id BIGINT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),

  CONSTRAINT uq_envio_proprio_codigo UNIQUE (codigo),
  CONSTRAINT uq_envio_proprio_id_saida UNIQUE (id_saida),
  CONSTRAINT ck_envio_proprio_origem CHECK (origem_remetente IN ('seller', 'manual'))
);

CREATE INDEX IF NOT EXISTS ix_envio_proprio_sub_base_created
  ON envio_proprio (sub_base, created_at DESC);

COMMENT ON TABLE envio_proprio IS
  'Snapshot imutável de etiqueta de envio próprio. codigo é globalmente único.';

-- Unicidade parcial RTE por tenant em saidas (mesmo código pode existir em N sub_bases)
CREATE UNIQUE INDEX IF NOT EXISTS uq_saidas_sub_base_codigo_rte
  ON saidas (sub_base, codigo)
  WHERE codigo ~ '^RTE[0-9]{11,}$';
