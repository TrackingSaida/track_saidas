# Migrações

Execute no **banco do Render** (e em qualquer ambiente) após deploy que inclua a coluna `tipo_owner` no modelo Owner.

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
