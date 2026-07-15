# Baseline — Tela Registros (D-15)

## Como medir (sem instrumentação nova)

1. Abrir Registros com DevTools Network + Performance.
2. Anotar `X-Backend-Process-Time` da chamada `GET /saidas/listar`.
3. Anotar duração total, TTFB, tamanho do payload e status HTTP.
4. Rodar `scripts/benchmark_registros.sql` na sub-base alvo.
5. Inventariar índices via query do script.
6. Repetir em 3 faixas de volume (baixa/média/alta) com o mesmo período D-15.

## Campos obrigatórios do baseline

| Métrica | Valor |
|---------|-------|
| sub_base | |
| de / ate | |
| p50 / p95 / p99 (ms) | |
| X-Backend-Process-Time (ms) | |
| tempo banco (EXPLAIN) | |
| tempo rede + render | |
| payload (bytes) | |
| linhas candidatas SQL | |
| linhas pós-filtro operacional | |
| nº de queries por request | |
| índices presentes | |

## Meta

Meta ainda não definida. Estabelecer alvo após o primeiro baseline real.

## Observações

- Não registrar códigos, cookies ou PII.
- Prefetch/cache só após otimização estrutural e se a meta não for atingida.
