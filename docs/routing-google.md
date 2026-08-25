# Roteirização Google Route Optimization

## Feature flags (Render)

| Variável | Valores | Default |
|----------|---------|---------|
| `ROUTING_OPTIMIZATION_PROVIDER` | `osrm` \| `google` | `osrm` |
| `ROUTING_GEOMETRY_PROVIDER` | `osrm` \| `google` | `osrm` |
| `ROUTING_GOOGLE_COST_OBJECTIVE` | `traveled_hour` \| `kilometer` | `traveled_hour` |
| `ROUTING_GOOGLE_TIMEOUT_S` | segundos | `30` |
| `GOOGLE_CLOUD_PROJECT` | project id | — |
| `GOOGLE_APPLICATION_CREDENTIALS` | path do JSON da service account | — |

**Produção:** manter `osrm`/`osrm` até a POC Euroville ser aprovada.

## Google Cloud

1. Habilitar **Route Optimization API**.
2. Service account exclusiva do backend.
3. Permissão exigida por `optimizeTours`: `routeoptimization.locations.use`.
4. Role `roles/routeoptimization.editor` pode ser usada na POC (não é “mínima”; agrega poderes extras). Preferir custom role só com `routeoptimization.locations.use` quando viável.
5. Credenciais **somente** no Render (secret file / ADC). Nunca no APK/git.

## Comportamento

- `optimization_mode` e `geometry_provider` são eixos independentes.
- Erro Google (timeout/429/5xx/auth) **não** cai em OSRM/NN.
- `priority_soft` continua local (deliberado).
- Idempotência: header `Idempotency-Key` + tabela `route_optimization_requests`.
- Geometria: CAS com `expected_route_revision` + `expected_geometry_order_hash`.
- `GET /rotas/ativa` só devolve polyline se `geometry_status=valid` e hash confere.

## Migration

```bash
psql "$DATABASE_URL" -f migrations/add_rotas_motoboy_routing_geometry.sql
```

## POC Euroville

```bash
GOOGLE_CLOUD_PROJECT=... python scripts/compare_osrm_google_route.py --fixture euroville
```

Validar trecho 4→5, custo traveled vs km, refreshDetailsRoutes e billing real no Console.

## Rollback

```text
ROUTING_OPTIMIZATION_PROVIDER=osrm
ROUTING_GEOMETRY_PROVIDER=osrm
```
