# track_saidas

## Migrações de banco de dados

As alterações de schema (colunas, tabelas) são aplicadas manualmente. Veja [migrations/README.md](migrations/README.md) para a lista de migrações e como executá-las (ex.: correção do erro `column entregador_fechamentos.id_motoboy does not exist`).

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