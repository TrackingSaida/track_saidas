# Migrações

## saidas_plataforma_cancelado_em.sql

**Obrigatória antes** do deploy da baixa automática de pedidos sincronizados. Sem ela a API
responde **500** nas listagens e no detalhe de saídas, porque o modelo já usa a coluna.
Aditiva e idempotente.

- `saidas.plataforma_cancelado_em`: quando o Mercado Livre/Shopee cancelou um pedido já
  coletado. O status não muda; a web mostra o aviso para o operador verificar a devolução.

```bash
psql "$DATABASE_URL" -f migrations/saidas_plataforma_cancelado_em.sql
```

## shopee_sync_pedidos.sql

**Obrigatória antes** do deploy do sync de pedidos Shopee. Sem ela a API responde **500**
no detalhe de saídas e nas rotas `/shopee`, porque os modelos já usam `shopee_tokens.id_base`
e `saidas.shopee_order_sn`. Aditiva e idempotente.

- `shopee_tokens.id_base`: vínculo da loja Shopee com o seller cadastrado; loja sem vínculo
  não é importada pelo sync.
- `saidas.shopee_order_sn`: nº do pedido Shopee (o código do pacote é o rastreio `BR…`).

```bash
psql "$DATABASE_URL" -f migrations/shopee_sync_pedidos.sql
```

## saidas_ml_pack_id.sql e ml_conexoes_id_base.sql

**Obrigatórias antes** do deploy dos ajustes da integração Mercado Livre. Sem elas a API
responde **500** no detalhe de saídas e nas rotas `/ml-int`, porque os modelos já usam
`saidas.ml_pack_id` e `ml_conexoes.id_base`. Aditivas e idempotentes.

- `saidas_ml_pack_id.sql`: Pack ID do ML na saída importada.
- `ml_conexoes_id_base.sql`: vínculo da conta ML com o seller cadastrado (`base.id_base`);
  conta sem vínculo não é importada pelo sync.

```bash
psql "$DATABASE_URL" -f migrations/saidas_ml_pack_id.sql
psql "$DATABASE_URL" -f migrations/ml_conexoes_id_base.sql
```

## pacote_g_categorias.sql

**Obrigatória antes** do deploy das categorias de pacote grande (G1, G2…). Sem ela a API
responde **500** em Registros (`/saidas/listar`), Preços de Entrega, Fechamento de Motoboys,
A Pagar e Fechamento de Bases, porque o modelo já usa `saidas.categoria_g_id`.

Cria `pacote_g_categoria` por `sub_base`, colunas `categoria_g_id` / `categoria_g_atualizado_*`
em `saidas`, `g_categorias` em `coletas`, `coleta_execucao_participantes` e
`base_fechamento_itens`, e `valor_g` em `base_fechamentos`. Backfill: cria a categoria "G"
para cada `sub_base` e liga os pacotes `is_grande` existentes a ela. Aditiva e idempotente.

```bash
psql "$DATABASE_URL" -f migrations/pacote_g_categorias.sql
```

## entregador_fechamento_unicidade.sql

Obrigatória no deploy do Fechamento de Motoboys v2 (geração em lote). Cria índices únicos
parciais que impedem dois fechamentos do mesmo motoboy/entregador no mesmo período
(a constraint antiga não protegia por causa do NULL). Aditiva e idempotente.
Se existirem duplicatas, a migration falha e lista os `id_fechamento` para correção manual.

```bash
psql "$DATABASE_URL" -f migrations/entregador_fechamento_unicidade.sql
```

Rollback (remove só os índices): `migrations/entregador_fechamento_unicidade_rollback.sql`.

## owner_cobranca_modulo.sql

Obrigatória antes do deploy de **Financeiro → Cobrança de Owners** (somente root).
Cria configuração comercial, preço por vigência, aceite de conferência, decisões,
fechamento versionado (snapshot, composição, itens, ajustes, histórico) e a trava
de unidade faturada. Aditiva e idempotente; não altera `owner_cobranca_itens`.

Seed: cada Owner recebe o modelo **sugerido** pelas flags (`ignorar_coleta`,
`entrada_obrigatoria_habilitada`) como `pendente_confirmacao`, e uma vigência de
preço aberta copiada de `owner.valor`. Nada é confirmado nem fechado sozinho.

```bash
psql "$DATABASE_URL" -f migrations/owner_cobranca_modulo.sql
# índice de leitura (CONCURRENTLY, fora de transação)
psql "$DATABASE_URL" -f migrations/owner_cobranca_indexes.sql
```

Rollback (apaga fechamentos de cobrança; só com confirmação explícita):
`migrations/owner_cobranca_modulo_rollback.sql`.

## owner_cobranca_v2.sql

Obrigatória antes do deploy da Cobrança de Owners v2. Permite desconto (R$ ou %),
acréscimo e isenção lançados dentro do período, antes do fechamento: colunas
`periodo_inicio`, `periodo_fim`, `percentual`, `removido_em`, `removido_por_username`
e tipo `isencao`. Aditiva e idempotente; requer `owner_cobranca_modulo.sql`.

```bash
psql "$DATABASE_URL" -f migrations/owner_cobranca_v2.sql
```

Rollback (apaga ajustes do período; só com confirmação explícita):
`migrations/owner_cobranca_v2_rollback.sql`.

## motoboy_convite_cadastro.sql

Obrigatória antes do deploy do autocadastro de motoboy por link de convite (PRD-004).
Cria `motoboy_convite` e `motoboy_cadastro_convite` (log). Aditiva e idempotente.

```bash
psql "$DATABASE_URL" -f migrations/motoboy_convite_cadastro.sql
```

## portal_seller_etiquetas.sql

Obrigatória para o Portal do Seller (PRD-001): área de cobertura, auditoria de envio próprio, limites e `seller_portal_access`.

```bash
psql "$DATABASE_URL" -f migrations/portal_seller_etiquetas.sql
```

## portal_seller_regioes_cobertura.sql

Aditiva ao Portal do Seller: tabela `cobertura_regiao` e coluna opcional `cobertura_cep_prefixo.id_regiao`.
Prefixos sem região continuam válidos no match (`CEP.startswith(prefixo)`). Lista vazia = atende qualquer CEP.

```bash
psql "$DATABASE_URL" -f migrations/portal_seller_regioes_cobertura.sql
```

## portal_seller_pedido_loja.sql

Coluna opcional `envio_proprio.pedido_loja` para o número do pedido do site (impresso na etiqueta).

```bash
psql "$DATABASE_URL" -f migrations/portal_seller_pedido_loja.sql
```

## coleta_flexivel.sql

Obrigatória antes do deploy da coleta flexível. Adiciona agenda por base, calendário de
exceções, execução/participantes de coleta, permissão canônica do motoboy, diária de
coleta e snapshots imutáveis no fechamento.
Também adiciona a baixa financeira `recebido_em/recebido_por` dos fechamentos de bases,
usada pela visão **A Receber**.

```bash
psql "$DATABASE_URL" -f migrations/coleta_flexivel.sql
```

O script preserva permissões existentes copiando `pode_ler_coleta=true` para
`pode_realizar_coleta=true`. Bases existentes começam com segunda a sábado, mas com a
agenda não confirmada; por isso não bloqueiam fechamento até revisão do administrador.

Execute no **banco do Render** (e em qualquer ambiente) após deploy que inclua a coluna correspondente no modelo.

## coleta_operacao_em_andamento.sql

Obrigatória após `coleta_flexivel.sql`. Adiciona os estados `em_coleta` e
`finalizado`, usados para reservar uma base, exibir quem está coletando e permitir
que outro motoboy ajude na mesma execução.

```bash
psql "$DATABASE_URL" -f migrations/coleta_operacao_em_andamento.sql
```

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

## create_push_notifications.sql

**Obrigatório** após deploy das notificações push mobile (tokens, preferências, digest, avisos da base, PDF de fechamento).

Cria:

- `device_push_tokens`
- `notif_prefs`
- `push_digest`
- `push_envio_log`
- `avisos_base` / `aviso_destinatarios`
- colunas `pdf_object_key` / `pdf_gerado_em` em `entregador_fechamentos`

```bash
psql "$DATABASE_URL" -f migrations/create_push_notifications.sql
```

Configuração dos Cron Jobs no Render: ver seção **Notificações push (mobile)** em [`README.md`](../README.md).

## ml_conexoes_unique_user_sub_base.sql

**Obrigatório** após deploy do fix de autenticação ML (invalid_grant + duplicatas).

1. Remove conexões duplicadas do mesmo seller na mesma `sub_base` (mantém a mais recente).
2. Cria índice único `(user_id_ml, sub_base)`.

```bash
psql "$DATABASE_URL" -f migrations/ml_conexoes_unique_user_sub_base.sql
```

Após o deploy, sellers com `invalid_grant` passam a aparecer como **Requer nova autorização** na tela de Autenticação.
