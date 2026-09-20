-- Regiões nomeadas de cobertura CEP (agrupam prefixos).
-- Aditiva e retrocompatível: prefixos sem região continuam válidos no match.

CREATE TABLE IF NOT EXISTS cobertura_regiao (
  id BIGSERIAL PRIMARY KEY,
  sub_base TEXT NOT NULL,
  nome TEXT NOT NULL,
  ativo BOOLEAN NOT NULL DEFAULT true,
  ordem INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_cobertura_regiao_sub_nome UNIQUE (sub_base, nome)
);

CREATE INDEX IF NOT EXISTS ix_cobertura_regiao_sub_base
  ON cobertura_regiao (sub_base);

ALTER TABLE cobertura_cep_prefixo
  ADD COLUMN IF NOT EXISTS id_regiao BIGINT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_cobertura_cep_prefixo_regiao'
  ) THEN
    ALTER TABLE cobertura_cep_prefixo
      ADD CONSTRAINT fk_cobertura_cep_prefixo_regiao
      FOREIGN KEY (id_regiao) REFERENCES cobertura_regiao(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_cobertura_cep_prefixo_id_regiao
  ON cobertura_cep_prefixo (id_regiao);

COMMENT ON TABLE cobertura_regiao IS
  'Agrupamento amigável de prefixos CEP (ex.: Barueri). Match continua por prefixo.';
COMMENT ON COLUMN cobertura_cep_prefixo.id_regiao IS
  'Região opcional; NULL = prefixo sem região nomeada.';
