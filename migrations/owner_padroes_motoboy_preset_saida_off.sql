-- Preset oficial Padrões do Motoboy (novos registros):
-- ler saídas + avulso coleta + foto; avulso saída off.
-- Não altera linhas existentes de owner/motoboys.

ALTER TABLE owner
  ALTER COLUMN default_pode_criar_avulso_saida SET DEFAULT false;

ALTER TABLE motoboys
  ALTER COLUMN pode_criar_avulso_saida SET DEFAULT false;
