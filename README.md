# track_saidas

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
6. **Restrict to file name prefix:** `saidas/` (obrigatório para o código).
7. Crie a chave e copie o **keyID** e a **applicationKey** (a chave só é exibida uma vez).

Use o **keyID** em `B2_ACCESS_KEY_ID` e a **applicationKey** em `B2_SECRET_ACCESS_KEY`.