-- PRD-012 Fase 4: motivo de sistema quando a rota aberta fica sem itens.
-- Aditiva. Aplicar antes de ligar confirmacao_saida_sessoes_habilitada.
-- Não foi executada neste passo.

BEGIN;

ALTER TABLE rotas_motoboy
    ADD COLUMN IF NOT EXISTS motivo_encerramento TEXT;

COMMIT;
