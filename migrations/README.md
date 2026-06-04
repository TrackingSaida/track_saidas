# Migrações

Execute no **banco do Render** (e em qualquer ambiente) após deploy que inclua a coluna correspondente no modelo.

## owner_nome_fantasia.sql

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
