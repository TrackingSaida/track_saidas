-- Adiciona data de nascimento opcional na tabela users (Aniversariantes do mês).
-- Execute uma única vez em cada ambiente.

ALTER TABLE users
ADD COLUMN IF NOT EXISTS data_nascimento DATE NULL;
