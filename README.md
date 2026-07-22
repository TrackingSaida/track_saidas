# track_saidas

## Logs críticos de leitura

Os logs de leitura usam a tabela existente `logs_leitura` e registram somente eventos críticos:

- `duplicado`
- `atribuido_a_outro`
- `assumiu_de_outro`

Campos importantes para consulta: `sub_base`, `username`, `codigo`, `resultado`, `motoboy_id`, `id_saida`, `origem_app`, `endpoint`, `created_at`.

### Consultas úteis

Eventos críticos do dia por usuário:

```sql
SELECT
  created_at,
  resultado,
  codigo,
  id_saida,
  motoboy_id,
  origem_app,
  endpoint
FROM logs_leitura
WHERE sub_base = 'Giro Express'
  AND username = 'USUARIO_AQUI'
  AND created_at >= date_trunc('day', now())
  AND created_at < date_trunc('day', now()) + interval '1 day'
ORDER BY created_at DESC;
```

Resumo diário por resultado:

```sql
SELECT
  resultado,
  COUNT(*) AS total
FROM logs_leitura
WHERE sub_base = 'Giro Express'
  AND created_at >= date_trunc('day', now())
  AND created_at < date_trunc('day', now()) + interval '1 day'
GROUP BY resultado
ORDER BY total DESC;
```

Join opcional com `saidas` por `id_saida`:

```sql
SELECT
  l.created_at,
  l.resultado,
  l.codigo AS codigo_log,
  l.id_saida,
  s.codigo AS codigo_saida,
  s.status,
  s.entregador
FROM logs_leitura l
LEFT JOIN saidas s ON s.id_saida = l.id_saida
WHERE l.sub_base = 'Giro Express'
  AND l.created_at >= date_trunc('day', now())
  AND l.created_at < date_trunc('day', now()) + interval '1 day'
ORDER BY l.created_at DESC;
```

## Migrações de banco de dados

As alterações de schema (colunas, tabelas) são aplicadas manualmente. Veja [migrations/README.md](migrations/README.md) para a lista de migrações e como executá-las (ex.: correção do erro `column entregador_fechamentos.id_motoboy does not exist`).

## Rotina de limpeza de histórico D-60 (Render)

### Escopo da v2

Fase A (por lote de `id_saida` antigo):

1. Purge B2 de comprovantes (`foto_url` em `saidas_detail`)
2. `saida_historico`
3. `saidas_detail`
4. `owner_cobranca_itens`
5. `logs_leitura` (por `id_saida`)
6. `saidas`

Fase B (tempo restante do cron, por data):

- `logs_leitura` (`created_at`)
- `rotas_motoboy` (finalizadas/canceladas)
- `address_telemetry`
- `geocode_cache`, `suggestion_cache`, `enderecos_conhecidos`
- `saidas_detail` / `owner_cobranca_itens` órfãos
- `coletas` órfãs (sem saídas filhas)

Fora do escopo: fechamentos financeiros, cadastro, tokens e configs.

Política de retenção: 60 dias (configurável). Execução: endpoint interno + Cron Job diário no Render. Política default em `history_retention_policy.sub_base='__global__'`.

### Pré-requisitos

1. Aplicar migração `migrations/history_cleanup_state.sql`.
2. (Opcional) Índices de performance: `migrations/cleanup_v2_optional_indexes.sql`.
3. Configurar variáveis no Web Service e no Cron Job:
   - `CRON_CLEANUP_SECRET` (obrigatória)
   - `HISTORY_RETENTION_DAYS` (default `60`)
   - `HISTORY_CLEANUP_BATCH_SIZE` (default `3000`)
   - `HISTORY_CLEANUP_MAX_RUNTIME_SECONDS` (default `540`)
   - `HISTORY_CLEANUP_B2_ENABLED` (default `true`; purge best-effort se credenciais B2 existirem)

### Endpoint interno

- `POST /api/internal/cleanup-history`
- Header obrigatório: `X-Cron-Secret: <CRON_CLEANUP_SECRET>`
- Resposta inclui `deleted.*` por tabela, `b2_objects.deleted/failed`, `partial`, `last_saida_id_checkpoint`, `remaining_estimate.before/after` e `skipped_tables` (tabelas opcionais ausentes no banco — ex.: caches de endereço não migrados)

Tabelas opcionais (`geocode_cache`, `suggestion_cache`, `enderecos_conhecidos`, `address_telemetry`, `rotas_motoboy`, `coletas`, etc.) são ignoradas com `deleted.*=0` quando não existem; o job continua e retorna HTTP 200 com o nome em `skipped_tables`.

### Auditoria antes/depois

Rodar `scripts/cleanup_orphans_audit.sql` no Postgres para baseline de órfãos e volume antigo.

### Agendamento recomendado no Render

Para rodar perto de 03:00 BRT (UTC-3), configurar cron em UTC:

- Schedule: `0 6 * * *`
- Command:

```bash
curl -sf -X POST \
  -H "X-Cron-Secret: $CRON_CLEANUP_SECRET" \
  "https://track-saidas-api.onrender.com/api/internal/cleanup-history"
```

Se o job não terminar no orçamento de tempo, ele retorna `partial=true` e continua no próximo dia a partir do checkpoint salvo.

## Encerramento de pendentes por quinzena (1.5.0+)

Encerra automaticamente pedidos ainda abertos (`SAIU_PARA_ENTREGA` / `EM_ROTA` / legado `saiu`) cuja **data operacional** está **antes** da janela viva de **2 quinzenas** (quinzena atual + quinzena anterior).

- Status resultante: `ENCERRADO_SISTEMA` (UI do status: “Encerrado”; última ação: “Encerrado pelo sistema”)
- Gera histórico com evento `encerrado_sistema`
- **Não** altera Ausente / Entregue / Cancelado
- Encerrado **não** conta como entrega paga; bipar de novo **pede confirmação** no app e, se confirmado, **reativa** o pedido

Execução: endpoint interno + Cron Job no Render (mesmo padrão do cleanup).

### Pré-requisitos

1. Deploy da API com a versão que inclui o endpoint (1.5.0+).
2. No Web Service (e no Cron Job, se usar variável própria), garantir ao menos um secret:
   - `CRON_ENCERRAMENTO_SECRET` (opcional; dedicado)
   - ou reutilizar `CRON_CLEANUP_SECRET` / `CRON_REFRESH_SECRET` (fallback já suportado pelo endpoint)

Não há migração de banco obrigatória para este job.

### Endpoint interno

- `POST /api/internal/encerrar-pendentes-quinzena`
- Header obrigatório: `X-Cron-Secret: <secret>`
- Query params:
  - `dry_run=true|false` (default `true` — só conta, **não** altera)
  - `batch_size` (default `500`, entre 50 e 2000)
  - `sub_base` (opcional — limita a uma sub_base)
  - `data=YYYY-MM-DD` (opcional — data de referência da janela; default = hoje)

Resposta útil para validar antes de aplicar:

- `inicio_vivo`, `ref_date`
- `candidatos`, `elegiveis`, `atualizados`
- `por_sub_base`, `sample_ids`

### Rollout recomendado no Render

1. Criar um **Cron Job** novo (ou um one-off manual) apontando para o Web Service da API.
2. **Primeira execução com dry-run** (obrigatório em produção):

```bash
curl -sf -X POST \
  -H "X-Cron-Secret: $CRON_CLEANUP_SECRET" \
  "https://track-saidas-api.onrender.com/api/internal/encerrar-pendentes-quinzena?dry_run=true"
```

3. Conferir `elegiveis` / `por_sub_base` / `sample_ids` nos logs ou na resposta.
4. Só então aplicar:

```bash
curl -sf -X POST \
  -H "X-Cron-Secret: $CRON_CLEANUP_SECRET" \
  "https://track-saidas-api.onrender.com/api/internal/encerrar-pendentes-quinzena?dry_run=false&batch_size=500"
```

### Agendamento recomendado

Diário de madrugada (UTC), por exemplo perto de 04:00 BRT:

- Schedule: `0 7 * * *`
- Command (produção, após validar dry-run):

```bash
curl -sf -X POST \
  -H "X-Cron-Secret: $CRON_CLEANUP_SECRET" \
  "https://track-saidas-api.onrender.com/api/internal/encerrar-pendentes-quinzena?dry_run=false&batch_size=500"
```

Dica: no Cron Job do Render, use a mesma env do cleanup (`CRON_CLEANUP_SECRET`) ou defina `CRON_ENCERRAMENTO_SECRET` e troque o header.

Filtro opcional por tenant na primeira limpeza grande:

```bash
".../encerrar-pendentes-quinzena?dry_run=true&sub_base=NOME_DA_SUB_BASE"
```

## Benchmark de performance (Registros)

Antes de ativar a limpeza e após alguns ciclos, rodar os mesmos testes em `GET /api/saidas/listar`:

```bash
# ajustar TOKEN e SUB_BASE
export BASE_URL="https://track-saidas-api.onrender.com"
export TOKEN="SEU_JWT"
export SUB_BASE="SUA_SUB_BASE"

# cenário 1: período padrão
curl -s -o /tmp/registros_periodo.json -w "http=%{http_code} total=%{time_total}s\n" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/api/saidas/listar?sub_base=$SUB_BASE&de=2026-05-01&ate=2026-05-31&limit=50&offset=0"

# cenário 2: cancelados no período
curl -s -o /tmp/registros_cancelados.json -w "http=%{http_code} total=%{time_total}s\n" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/api/saidas/listar?sub_base=$SUB_BASE&status=cancelado&de=2026-05-01&ate=2026-05-31&limit=50&offset=0"
```

No banco, capturar plano/tempo para comparação:

```sql
EXPLAIN ANALYZE
SELECT id_saida, timestamp, codigo, status
FROM saidas
WHERE sub_base = 'SUA_SUB_BASE'
  AND timestamp >= '2026-05-01'
  AND timestamp <  '2026-06-01'
ORDER BY timestamp DESC
LIMIT 50 OFFSET 0;
```

## Configuração do Backblaze B2 (fotos de entrega)

O upload de fotos de entrega/ausente usa um bucket B2 privado. O backend só gera URLs presigned se as credenciais estiverem configuradas.

### Variáveis de ambiente

| Variável | Obrigatório | Descrição |
|----------|-------------|-----------|
| `B2_BUCKET_NAME` | Sim* | Nome do bucket (ex.: `ts-prod-entregas-fotos`) |
| `B2_ACCESS_KEY_ID` | Sim | **Application Key ID** da chave B2 (keyID) |
| `B2_SECRET_ACCESS_KEY` | Sim | **Application Key** (secret) da chave B2 |
| `B2_ENDPOINT_URL` | Não | Endpoint S3 do B2 (default: `https://s3.us-east-005.backblazeb2.com`) |

\* Se não informado, o código usa o default `ts-prod-entregas-fotos`.

### Onde configurar

**1. Desenvolvimento local**

Crie um arquivo `.env` na raiz do projeto (copie de `.env.example`) e preencha:

```bash
B2_BUCKET_NAME=ts-prod-entregas-fotos
B2_ACCESS_KEY_ID=00504efd17d95b60000000001
B2_SECRET_ACCESS_KEY=sua_application_key_aqui
B2_ENDPOINT_URL=https://s3.us-east-005.backblazeb2.com
```

Reinicie o servidor (uvicorn) após alterar o `.env`.

**2. Render (produção)**

1. Acesse o dashboard do [Render](https://dashboard.render.com).
2. Abra o serviço **Web Service** da API (ex.: track-saidas-api).
3. Vá em **Environment** (menu lateral).
4. Clique em **Add Environment Variable** e adicione cada uma:
   - `B2_BUCKET_NAME` = `ts-prod-entregas-fotos`
   - `B2_ACCESS_KEY_ID` = (keyID da Application Key do B2)
   - `B2_SECRET_ACCESS_KEY` = (applicationKey da Application Key do B2)
   - `B2_ENDPOINT_URL` = `https://s3.us-east-005.backblazeb2.com`
5. Salve. O Render faz um novo deploy automaticamente; aguarde terminar.

### Como obter a Application Key no Backblaze B2

1. Acesse [Backblaze B2](https://www.backblaze.com/b2/), faça login e abra **Application Keys**.
2. Clique em **Add a New Application Key**.
3. Nome: ex. `tracking-saida-prod`.
4. **Allow access to Bucket(s):** selecione o bucket `ts-prod-entregas-fotos`.
5. **Type of Access:** Read and Write.
6. **Restrict to file name prefix:** `saida/` (obrigatório para o código).
7. Crie a chave e copie o **keyID** e a **applicationKey** (a chave só é exibida uma vez).

Use o **keyID** em `B2_ACCESS_KEY_ID` e a **applicationKey** em `B2_SECRET_ACCESS_KEY`.

### Erro 403 "AccessDenied / not entitled" no upload (mobile)

Se o app mostrar **"Upload recusado (403)"** com mensagem `AccessDenied` ou `not entitled`, o B2 está recusando o PUT na URL presigned. Corrija a Application Key:

1. **Tipo de acesso:** use **Read and Write** (não use só "Write Only"; presigned PUT pode exigir Read and Write).
2. **Bucket:** a chave deve ter acesso ao bucket `ts-prod-entregas-fotos` (ou o valor de `B2_BUCKET_NAME`).
3. **Restrição de prefixo:** em "Restrict to file name prefix" use exatamente `saida/` (com a barra). As chaves geradas pelo backend são do tipo `saida/{id}/{tipo}/{uuid}.jpg`.
4. Crie uma **nova** Application Key com essas opções, atualize `B2_ACCESS_KEY_ID` e `B2_SECRET_ACCESS_KEY` no ambiente (Render ou .env) e faça um novo deploy/reinício.