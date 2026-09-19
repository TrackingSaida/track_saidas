-- Portal do Seller + Área de Cobertura + auditoria de envio próprio
-- Idempotente. Aditivo.

-- ============================================================
-- Owner: limites de etiqueta
-- ============================================================
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS etiqueta_limite_diario_default INTEGER NOT NULL DEFAULT 50;

ALTER TABLE owner
ADD COLUMN IF NOT EXISTS etiqueta_expiracao_dias INTEGER NOT NULL DEFAULT 30;

-- ============================================================
-- Base: teto diário opcional por seller
-- ============================================================
ALTER TABLE base
ADD COLUMN IF NOT EXISTS etiqueta_limite_diario INTEGER NULL;

-- ============================================================
-- envio_proprio: origem e cancelamento
-- ============================================================
ALTER TABLE envio_proprio
ADD COLUMN IF NOT EXISTS origem_emissao TEXT NOT NULL DEFAULT 'staff';

ALTER TABLE envio_proprio
ADD COLUMN IF NOT EXISTS cancelado_at TIMESTAMP WITHOUT TIME ZONE NULL;

ALTER TABLE envio_proprio
ADD COLUMN IF NOT EXISTS cancelado_por TEXT NULL;

CREATE INDEX IF NOT EXISTS ix_envio_proprio_sub_base_created
  ON envio_proprio (sub_base, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_envio_proprio_id_base_created
  ON envio_proprio (id_base, created_at DESC);

-- ============================================================
-- Prefixos de CEP (Área de Cobertura)
-- ============================================================
CREATE TABLE IF NOT EXISTS cobertura_cep_prefixo (
  id BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  prefixo TEXT NOT NULL,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_cobertura_cep_sub_base_prefixo UNIQUE (sub_base, prefixo)
);

CREATE INDEX IF NOT EXISTS ix_cobertura_cep_sub_base
  ON cobertura_cep_prefixo (sub_base);

-- ============================================================
-- Acesso ao portal do seller
-- ============================================================
CREATE TABLE IF NOT EXISTS seller_portal_access (
  id BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  id_base BIGINT NOT NULL REFERENCES base(id_base) ON DELETE CASCADE,
  login TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
  criado_por_user_id BIGINT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  last_login_at TIMESTAMP WITHOUT TIME ZONE NULL,
  CONSTRAINT uq_seller_portal_login UNIQUE (login),
  CONSTRAINT uq_seller_portal_sub_base_login UNIQUE (sub_base, login)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_seller_portal_ativo_id_base
  ON seller_portal_access (id_base)
  WHERE ativo IS TRUE;

CREATE INDEX IF NOT EXISTS ix_seller_portal_sub_base
  ON seller_portal_access (sub_base);
