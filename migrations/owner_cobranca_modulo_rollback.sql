-- Rollback da Cobrança de Owners. Remove SOMENTE as tabelas novas do módulo.
-- Apaga fechamentos de cobrança gravados: execute apenas com confirmação explícita.
-- Não toca em owner, saidas, coletas, saida_historico nem owner_cobranca_itens.

BEGIN;

DROP TABLE IF EXISTS owner_unidade_faturada;
DROP TABLE IF EXISTS owner_fechamento_historico;
DROP TABLE IF EXISTS owner_fechamento_ajuste;
DROP TABLE IF EXISTS owner_fechamento_item;
DROP TABLE IF EXISTS owner_fechamento_composicao;
DROP TABLE IF EXISTS owner_fechamento;
DROP TABLE IF EXISTS owner_apuracao_decisao;
DROP TABLE IF EXISTS owner_apuracao;
DROP TABLE IF EXISTS owner_preco_vigencia;
DROP TABLE IF EXISTS owner_configuracao_cobranca;

COMMIT;
