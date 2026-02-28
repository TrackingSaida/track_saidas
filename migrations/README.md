# Migrações de banco de dados

As migrações deste projeto são aplicadas **manualmente**. Não há runner automático (Alembic, etc.); execute os arquivos `.sql` no PostgreSQL usado pela API (ex.: produção no Render) quando for necessário.

## Como executar

Use a connection string do banco (ex.: variável de ambiente do Render ou URL local):

```bash
psql "postgresql://user:password@host:port/database" -f migrations/NOME_DO_ARQUIVO.sql
```

Ou abra o arquivo `.sql` em um cliente gráfico (DBeaver, pgAdmin, etc.) e execute o conteúdo na ordem.

## Migrações relevantes (ordem sugerida)

| Arquivo | Descrição | Pré-requisito |
|---------|-----------|---------------|
| `motoboy_perfil.sql` | Perfil motoboy (role 4), tabela `motoboy_sub_base`, colunas em `motoboys`. | Tabela `motoboys` já existente. |
| `entregador_fechamento_id_motoboy.sql` | Adiciona coluna `id_motoboy` em `entregador_fechamentos` para Fechamento de Motoboys. | Tabela `motoboys` existente (ex.: após `motoboy_perfil.sql` ou schema inicial). |

### Erro "column entregador_fechamentos.id_motoboy does not exist"

Se a tela **Fechamento de Motoboys** retornar esse erro, aplique a migração:

```bash
psql "<CONNECTION_STRING>" -f migrations/entregador_fechamento_id_motoboy.sql
```

Depois reinicie a API ou aguarde o próximo deploy e teste novamente.
