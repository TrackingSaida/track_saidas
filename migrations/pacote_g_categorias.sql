-- Categorias de pacote G (G1, G2, G3...) por sub_base, com regra de pagamento
-- ao motoboy e de cobrança da base. Aditiva e idempotente.

BEGIN;

CREATE TABLE IF NOT EXISTS pacote_g_categoria (
    id BIGSERIAL PRIMARY KEY,
    sub_base TEXT NOT NULL,
    nome TEXT NOT NULL,
    descricao TEXT NULL,
    motoboy_modo TEXT NOT NULL DEFAULT 'multiplicador'
        CHECK (motoboy_modo IN ('multiplicador', 'valor_fixo')),
    motoboy_valor NUMERIC(12,2) NOT NULL DEFAULT 1.00 CHECK (motoboy_valor >= 0),
    base_modo TEXT NOT NULL DEFAULT 'valor_fixo'
        CHECK (base_modo IN ('multiplicador', 'valor_fixo')),
    base_valor NUMERIC(12,2) NOT NULL DEFAULT 0.00 CHECK (base_valor >= 0),
    ordem INTEGER NOT NULL DEFAULT 0,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pacote_g_categoria_nome UNIQUE (sub_base, nome)
);

CREATE INDEX IF NOT EXISTS ix_pacote_g_categoria_sub_base
    ON pacote_g_categoria (sub_base, ativo, ordem);

ALTER TABLE saidas
    ADD COLUMN IF NOT EXISTS categoria_g_id BIGINT NULL
        REFERENCES pacote_g_categoria(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS categoria_g_atualizado_em TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS categoria_g_atualizado_por BIGINT NULL;

CREATE INDEX IF NOT EXISTS ix_saidas_categoria_g
    ON saidas (sub_base, categoria_g_id) WHERE categoria_g_id IS NOT NULL;

-- {"<categoria_id>": {"shopee": n, "ml": n, "avulso": n}}
ALTER TABLE coletas
    ADD COLUMN IF NOT EXISTS g_categorias JSONB NULL;

ALTER TABLE coleta_execucao_participantes
    ADD COLUMN IF NOT EXISTS g_categorias JSONB NULL;

ALTER TABLE base_fechamento_itens
    ADD COLUMN IF NOT EXISTS g_categorias JSONB NULL;

ALTER TABLE base_fechamentos
    ADD COLUMN IF NOT EXISTS valor_g NUMERIC(12,2) NOT NULL DEFAULT 0.00;

-- Categoria inicial "G" reproduz a regra atual: motoboy +1x o serviço; base sem cobrança automática.
INSERT INTO pacote_g_categoria (sub_base, nome, descricao, motoboy_modo, motoboy_valor, base_modo, base_valor, ordem)
SELECT DISTINCT o.sub_base, 'G', 'Pacote grande', 'multiplicador', 1.00, 'valor_fixo', 0.00, 0
FROM owner o
WHERE o.sub_base IS NOT NULL AND TRIM(o.sub_base) <> ''
ON CONFLICT (sub_base, nome) DO NOTHING;

UPDATE saidas s
SET categoria_g_id = c.id
FROM pacote_g_categoria c
WHERE s.is_grande IS TRUE
  AND s.categoria_g_id IS NULL
  AND c.sub_base = s.sub_base
  AND c.nome = 'G';

COMMIT;
