# Avaliação de índices — GET /saidas/listar (pós etapas 2 e 3)

## Decisão desta entrega

**Não criar índice novo nesta entrega.**

Motivo: o playbook exige `EXPLAIN (ANALYZE, BUFFERS)` da consulta final (página + agregações)
antes de criar índice. Sem acesso ao Postgres de produção/staging nesta implementação,
criar índice seria especulação (anti-pattern 001).

## Índices já documentados (aplicar se ausentes)

Arquivo: `migrations/saidas_listar_performance_indexes.sql`

- `ix_saidas_sub_base_timestamp` — pré-filtro D-15 por `sub_base + timestamp`
- `ix_saidas_sub_base_codigo` — fast path código exato
- `ix_saida_historico_saida_ts` — carga de histórico por `id_saida`
- `ix_saida_historico_evento_ts` — suporte parcial ao EXISTS por evento

## Próximo passo operacional

1. Rodar `scripts/benchmark_registros.sql` após deploy do código otimizado.
2. Confirmar se os índices acima existem (`pg_indexes`).
3. Se o plano ainda mostrar Seq Scan custoso em `saida_historico` no EXISTS,
   avaliar índice composto `(id_saida, evento, timestamp)` em migration nova com `CONCURRENTLY`.
4. Rollback: manter índice se criado; só dropar com evidência de impacto negativo em escrita.

## Critério de saída desta etapa

- Avaliação documentada.
- Nenhum índice novo sem EXPLAIN da consulta final.
