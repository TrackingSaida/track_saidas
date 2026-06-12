CREATE TABLE IF NOT EXISTS enderecos_conhecidos (
  id SERIAL PRIMARY KEY,
  sub_base VARCHAR(32) NOT NULL,
  motoboy_id INT,
  rua TEXT NOT NULL,
  numero VARCHAR(32),
  bairro TEXT,
  cidade TEXT NOT NULL,
  estado VARCHAR(2) NOT NULL,
  cep VARCHAR(16),
  latitude DOUBLE PRECISION NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  qtd_utilizacoes INT DEFAULT 1,
  ultima_utilizacao TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_end_conhecidos_sub_base ON enderecos_conhecidos (sub_base);
CREATE INDEX IF NOT EXISTS idx_end_conhecidos_motoboy ON enderecos_conhecidos (motoboy_id, ultima_utilizacao);
CREATE INDEX IF NOT EXISTS idx_end_conhecidos_cep ON enderecos_conhecidos (sub_base, cep);
