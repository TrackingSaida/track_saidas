-- ML Int: adiciona nome do usuário (nickname) em ml_conexoes
-- Executar uma vez no banco após ml_int_tables.sql

ALTER TABLE ml_conexoes ADD COLUMN IF NOT EXISTS user_nickname_ml TEXT;
