# Migrações

Execute no **banco do Render** (e em qualquer ambiente) após deploy que inclua a coluna correspondente no modelo.

## motoboy_refresh_tokens.sql + rotas_motoboy_continuidade.sql

**Obrigatório** após deploy da sessão estável + continuidade de rota (PR backend).

1. `migrations/motoboy_refresh_tokens.sql` — tabela de refresh tokens mobile
2. `migrations/rotas_motoboy_continuidade.sql` — colunas `sub_base`, `updated_at`, índice UNIQUE parcial

Ordem sugerida no Render Shell:

```bash
psql "$DATABASE_URL" -f migrations/motoboy_refresh_tokens.sql
psql "$DATABASE_URL" -f migrations/rotas_motoboy_continuidade.sql
```

Se o índice UNIQUE falhar com duplicatas, rode antes `scripts/fix_rotas_motoboy_duplicadas_abertas.sql` e tente o `CREATE UNIQUE INDEX` novamente.

Variáveis de ambiente novas (`.env.example`):

- `MOTOBOY_ACCESS_TOKEN_EXPIRE_DAYS=30`
- `MOTOBOY_REFRESH_TOKEN_EXPIRE_DAYS=90`

Auditar em produção: `ACCESS_TOKEN_EXPIRE_MINUTES` e estabilidade de `SECRET_KEY`.


Adiciona a coluna opcional `nome_fantasia` na tabela `owner` para o campo institucional "Emitido por" no relatório de fechamento. Não afeta registros nem índices existentes.

```sql
ALTER TABLE owner ADD COLUMN IF NOT EXISTS nome_fantasia TEXT NULL;
```

## add_tipo_owner_to_owner.sql

**Obrigatório** após o deploy que adicionou "tipo do owner" (BASE/SUBBASE).

Se não rodar, a API pode retornar **500** em login, listagem de owners, `/api/owner/me` e `/api/ui/menu` (quando o token for renovado), pois o modelo espera a coluna `owner.tipo_owner`.

**No Render:**

1. Dashboard do serviço → aba **Shell** (ou use um cliente PostgreSQL com a connection string do Render).
2. Conecte ao banco e execute o conteúdo de `add_tipo_owner_to_owner.sql`:

```sql
ALTER TABLE owner
ADD COLUMN IF NOT EXISTS tipo_owner TEXT NOT NULL DEFAULT 'subbase';
```

Ou, se o Render não tiver Shell, use a **connection string** em um cliente (DBeaver, psql, etc.) e rode o arquivo `.sql`.

## history_cleanup_state.sql

Cria as tabelas da rotina de limpeza de histórico D-60:

- `maintenance_job_state`: checkpoint de execução para retomada automática por janela.
- `history_retention_policy`: política de retenção (v1 global em 60 dias com chave `__global__`, preparada para futuro por `sub_base`).

Executar:

```sql
-- arquivo: migrations/history_cleanup_state.sql
```

## cleanup_v2_optional_indexes.sql

**Opcional** após deploy da limpeza D-60 v2. Acelera deletes por data na Fase B (`logs_leitura`, `rotas_motoboy`).

```sql
-- arquivo: migrations/cleanup_v2_optional_indexes.sql
```

## pedido_campos_obrigatorios_config.sql

Cria a tabela de configuração por sub-base para campos obrigatórios na conclusão de pedido:

- serviço (`Shopee`, `Mercado Livre`, `Avulso`);
- contexto (`ENTREGUE`, `AUSENTE`, `AMBOS`);
- lista de campos obrigatórios (`campos_obrigatorios` em JSON texto);
- status ativo/inativo.

Executar:

```sql
-- arquivo: migrations/pedido_campos_obrigatorios_config.sql
```

## Busca inteligente de endereços (mobile — sugestões Google)

**Obrigatório** após deploy da PR #19 (`mobile/enderecos/sugestoes`). Sem estas tabelas o endpoint responde, mas com sugestões degradadas/vazias.

Executar **nesta ordem** no Postgres (Render Shell ou cliente externo):

1. `geocode_cache.sql`
2. `suggestion_cache.sql`
3. `enderecos_conhecidos.sql`
4. `address_telemetry.sql`
5. `geocode_metadata.sql`
6. `coord_precision.sql`

**Variáveis de ambiente no Render (recomendado):**

- `GOOGLE_PLACES_API_KEY` — fallback Google Places para autocomplete

**Validar deploy da API:**

```bash
./scripts/verify_openapi.sh
# ou:
curl -s https://track-saidas-api.onrender.com/api/openapi.json | grep enderecos/sugestoes
```

Deve aparecer `/api/mobile/enderecos/sugestoes`. Se não aparecer, o Render ainda está em versão antiga — faça **Manual Deploy → Deploy latest commit** na branch `main`.

## saidas_listar_performance_indexes.sql

**Recomendado** para performance da tela **Registros** (`GET /saidas/listar`), especialmente busca exata por código (`codigo_exato=true`).

Inclui índice composto `(sub_base, codigo)` usado pelo fast path do endpoint.

Antes de criar índice **UNIQUE** em `(sub_base, codigo)`, rodar a auditoria de duplicados em [`scripts/saidas_codigo_index_audit.sql`](../scripts/saidas_codigo_index_audit.sql).

```sql
-- arquivo: migrations/saidas_listar_performance_indexes.sql
```

## logs_leitura_dedup_index.sql

**Recomendado** após bipagem concorrente: acelera o SELECT de dedup em `registrar_log_leitura_critico` (janela de poucos segundos).

```sql
-- arquivo: migrations/logs_leitura_dedup_index.sql
-- Rodar no Postgres (fora de transação, CONCURRENTLY):
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_logs_leitura_dedup_critico
    ON logs_leitura (sub_base, username, tipo, resultado, codigo, id_saida, motoboy_id, created_at DESC);
```

