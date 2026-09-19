-- users.email opcional: login por username/contato.
-- Strings vazias quebram UNIQUE clássico; múltiplos NULL são permitidos no PostgreSQL.
UPDATE users
SET email = NULL
WHERE email IS NOT NULL AND btrim(email) = '';

ALTER TABLE users
    ALTER COLUMN email DROP NOT NULL;

COMMENT ON COLUMN users.email IS 'E-mail opcional do usuário. NULL quando não informado.';
