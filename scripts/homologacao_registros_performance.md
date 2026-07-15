# Homologação — Performance Registros (Etapa 7)

## Escopo liberável

| Repositório | Conteúdo |
|-------------|----------|
| `track_saidas` | Paginação/agregação via `saidas_listar_service`, equivalência operacional, baseline/scripts |
| `track_saidas_html` | Listagem sem esperar combos (5A); `ensureAuthUser` compartilhado (5B) |
| `track_saida_mobile` | Sem alteração; validar se algum fluxo chama `GET /saidas/listar` |

## Ordem de deploy

1. Backend `track_saidas` (contrato preservado: `total`, `sumShopee`, `sumMercado`, `sumAvulso`, `items`).
2. Validar equivalência e tempos no backend.
3. Frontend `track_saidas_html`.
4. Índice novo: **não** nesta entrega (ver `indices_listar_avaliacao.md`).

## Checklist funcional

- [ ] D-15: primeira página, ordem, total e totalizadores coerentes com matriz/testes.
- [ ] Filtros combinados: base, status, serviço, ação, entregador, código, pacote G.
- [ ] Fast path código exato tenant-safe (sem consulta ampla).
- [ ] Duas sub-bases com códigos coincidentes: isolamento preservado.
- [ ] Detalhe / histórico / edição / exclusão na tela Registros.
- [ ] Perfis admin e operador; motoboy se aplicável na web.
- [ ] Coluna/filtro G respeita permissão após `ensureAuthUser`.
- [ ] Waterfall: listagem inicia sem aguardar `/base/` e `/users/motoboys`.
- [ ] No máximo uma chamada efetiva de `/auth/me` na abertura (promessa compartilhada).
- [ ] Mobile: sem regressão se consumir o mesmo envelope.

## Checklist de performance (comparar ao baseline)

Cenário: mesma `sub_base`, mesmo D-15, `limit=50`, `offset=0`.

| Métrica | Baseline | Depois | OK? |
|---------|----------|--------|-----|
| p50 endpoint | | | |
| p95 endpoint | | | |
| Tempo banco | | | |
| Tempo backend (`X-Backend-Process-Time`) | | | |
| Payload (bytes) | | | |
| Linhas candidatas / pós-filtro | | | |
| Nº queries | | | |
| Tempo até tabela renderizada | | | |

Ferramentas: `scripts/benchmark_registros.sql`, DevTools Network/Performance, logs Render.

## Observação pós-deploy (janela acordada)

- [ ] Sem aumento relevante de 4xx/5xx / timeouts.
- [ ] CPU/RAM e conexões DB estáveis.
- [ ] Sem degradação mensurável em escrita de histórico (se índice for criado depois).

## Rollback

- Backend: deploy anterior de `track_saidas` (sem migration destrutiva).
- Frontend: deploy anterior de `track_saidas_html`.
- Prefetch: N/A (não entregue).
- Índice: N/A nesta entrega.

## Commits sugeridos (separados por repositório)

**track_saidas**
```
feat: pagina listagem de registros antes do enriquecimento

Otimiza GET /saidas/listar com helper de filtro operacional,
paginação LIMIT/OFFSET e totalizadores no mesmo conjunto lógico.
```

**track_saidas_html**
```
perf: inicia listagem de registros sem aguardar combos

Desacopla refresh inicial dos selects e deduplica /auth/me via ensureAuthUser.
```

## Critério de saída

Equivalência funcional aprovada + ganho mensurável vs baseline + sem regressão operacional na janela de observação.
