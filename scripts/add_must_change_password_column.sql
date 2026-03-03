-- Adiciona a coluna must_change_password na tabela users.
-- Execute uma única vez em cada ambiente (ex.: no Render: Dashboard > PostgreSQL > Connect > executar este SQL).

ALTER TABLE users
ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT TRUE;
