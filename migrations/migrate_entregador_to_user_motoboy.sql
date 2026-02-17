-- =============================================================================
-- Migração: Entregador -> User + Motoboy (PostgreSQL)
-- =============================================================================
--
-- COMO EXECUTAR:
--
-- 1) Via psql (linha de comando):
--    cd track_saidas
--    psql -h HOST -U USUARIO -d NOME_BANCO -f migrations/migrate_entregador_to_user_motoboy.sql
--
-- 2) Via DBeaver, pgAdmin ou outro cliente:
--    Abra o arquivo e execute todo o conteúdo no banco.
--
-- 3) Via variável de ambiente (se DATABASE_URL estiver definida):
--    psql $DATABASE_URL -f migrations/migrate_entregador_to_user_motoboy.sql
--
-- Requer: extensão pgcrypto (criada automaticamente no script).
-- Senha padrão dos usuários migrados: migrado_trocar_senha
-- Idempotente: entregadores já migrados são ignorados.
--
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
DECLARE
  r RECORD;
  new_user_id BIGINT;
  new_motoboy_id BIGINT;
  senha_hash TEXT;
BEGIN
  senha_hash := crypt('migrado_trocar_senha', gen_salt('bf'));

  FOR r IN
    SELECT id_entregador, sub_base, nome, telefone, documento, ativo, data_cadastro,
           rua, numero, complemento, cep, cidade, bairro, coletador, username_entregador
    FROM entregador
    ORDER BY id_entregador
  LOOP
    -- Já migrado?
    IF EXISTS (SELECT 1 FROM users WHERE email = 'entregador_' || r.id_entregador || '@migrado.local') THEN
      CONTINUE;
    END IF;

    -- Inserir User
    INSERT INTO users (
      email, password_hash, username, contato, nome, sobrenome,
      status, sub_base, coletador, username_entregador, role
    )
    VALUES (
      'entregador_' || r.id_entregador || '@migrado.local',
      senha_hash,
      LEFT(COALESCE(NULLIF(TRIM(r.username_entregador), ''), NULLIF(TRIM(r.nome), ''), 'entregador_' || r.id_entregador), 100),
      LEFT(COALESCE(NULLIF(TRIM(r.telefone), ''), '0000000000'), 50),
      NULLIF(LEFT(TRIM(COALESCE(r.nome, '')), 100), ''),
      NULL,
      COALESCE(r.ativo, false),
      r.sub_base,
      COALESCE(r.coletador, false),
      r.username_entregador,
      4
    )
    RETURNING id INTO new_user_id;

    -- Inserir Motoboy
    INSERT INTO motoboys (
      user_id, sub_base, documento, rua, numero, complemento, bairro, cidade, estado, cep,
      ativo, data_cadastro, pode_ler_coleta, pode_ler_saida
    )
    VALUES (
      new_user_id,
      r.sub_base,
      r.documento,
      COALESCE(TRIM(r.rua), ''),
      COALESCE(TRIM(r.numero), ''),
      NULLIF(TRIM(COALESCE(r.complemento, '')), ''),
      COALESCE(TRIM(r.bairro), ''),
      COALESCE(TRIM(r.cidade), ''),
      NULL,
      COALESCE(TRIM(r.cep), '00000000'),
      COALESCE(r.ativo, true),
      COALESCE(r.data_cadastro, CURRENT_DATE),
      COALESCE(r.coletador, false),
      true
    )
    RETURNING id_motoboy INTO new_motoboy_id;

    -- Inserir MotoboySubBase
    IF r.sub_base IS NOT NULL AND TRIM(r.sub_base) != '' THEN
      INSERT INTO motoboy_sub_base (motoboy_id, sub_base, ativo)
      VALUES (new_motoboy_id, TRIM(r.sub_base), true);
    END IF;

    -- Atualizar Saidas com entregador_id
    UPDATE saidas
    SET motoboy_id = new_motoboy_id
    WHERE entregador_id = r.id_entregador
      AND (motoboy_id IS NULL OR motoboy_id != new_motoboy_id);

  END LOOP;
END $$;

-- Atualizar Saidas que têm apenas entregador (texto) e sem entregador_id
-- Faz match por sub_base + nome (lower, sem acentos simplificado)
WITH motoboys_por_nome AS (
  SELECT mo.id_motoboy, u.sub_base,
    LOWER(TRIM(TRANSLATE(COALESCE(u.nome, ''), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'))) AS nome_norm
  FROM motoboys mo
  JOIN users u ON u.id = mo.user_id
  WHERE u.email LIKE 'entregador_%@migrado.local'
),
motoboys_unicos AS (
  SELECT DISTINCT ON (sub_base, nome_norm) id_motoboy, sub_base, nome_norm
  FROM motoboys_por_nome
  ORDER BY sub_base, nome_norm, id_motoboy
)
UPDATE saidas s
SET motoboy_id = m.id_motoboy
FROM motoboys_unicos m
WHERE s.entregador_id IS NULL
  AND s.motoboy_id IS NULL
  AND s.entregador IS NOT NULL
  AND TRIM(s.entregador) != ''
  AND s.sub_base = m.sub_base
  AND LOWER(TRIM(TRANSLATE(s.entregador, 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'))) = m.nome_norm;
