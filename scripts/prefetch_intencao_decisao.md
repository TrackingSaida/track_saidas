# Prefetch por intenção — decisão (Etapa 6)

## Critério de entrada (plano)

Avaliar prefetch da primeira página D-15 **somente se**, após as etapas 2–5,
o tempo percebido da primeira página permanecer acima da meta definida a partir do baseline.

## Decisão desta entrega

**Não implementar prefetch/cache nesta entrega.**

Motivos:
1. Baseline ponta a ponta e meta numérica pós-otimização ainda dependem de medição
   em staging/produção com o backend novo (`listar_saidas_paginado`).
2. Sem evidência de que o backend otimizado + listagem desbloqueada (5A) ainda
   deixam a abertura acima da meta, prefetch seria custo/risco prematuro
   (dados operacionais desatualizados, corrida com filtros, carga extra).
3. Prefetch automático após login permanece **fora de escopo** (decisão técnica do plano).

## Como reavaliar depois do deploy

1. Rodar checklist de homologação (`scripts/homologacao_registros_performance.md`).
2. Medir tempo até tabela renderizada (p50/p95) no cenário D-15 de referência.
3. Se a meta do baseline **não** for atingida, aí sim abrir entrega específica de
   prefetch por intenção (hover/click no menu), com:
   - chave por `sub_base` + usuário + filtros + página + pageSize;
   - TTL curto em memória da aba;
   - promise compartilhada + AbortController;
   - invalidação ao editar/excluir na própria tela;
   - flag desligável.

## Critério de saída desta etapa

- Decisão documentada (não implementar agora).
- Nenhum código de prefetch/cache adicionado.
