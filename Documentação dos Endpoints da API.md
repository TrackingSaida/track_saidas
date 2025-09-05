# Documentação dos Endpoints da API

Esta API permite receber e armazenar dados JSON de forma simples e flexível.

## Base URL
```
http://localhost:5000/api
```

## Endpoints Disponíveis

### 1. Endpoints Específicos

#### POST /api/dados
Recebe dados genéricos e grava no banco de dados.

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/dados \
  -H "Content-Type: application/json" \
  -d '{"nome": "João", "idade": 30, "cidade": "São Paulo"}'
```

**Resposta de sucesso:**
```json
{
  "sucesso": true,
  "id": 1,
  "mensagem": "Dados gravados com sucesso"
}
```

#### GET /api/dados
Lista todos os dados gravados no endpoint "dados".

**Exemplo de requisição:**
```bash
curl http://localhost:5000/api/dados
```

#### POST /api/formulario
Recebe dados de formulário e grava no banco de dados.

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/formulario \
  -H "Content-Type: application/json" \
  -d '{"nome": "Maria", "email": "maria@email.com", "mensagem": "Olá!"}'
```

#### GET /api/formulario
Lista todos os formulários gravados.

#### POST /api/contato
Recebe dados de contato e grava no banco de dados.

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/contato \
  -H "Content-Type: application/json" \
  -d '{"nome": "Pedro", "telefone": "11999999999", "assunto": "Dúvida"}'
```

#### GET /api/contato
Lista todos os contatos gravados.

### 2. Endpoints Genéricos

#### POST /api/salvar/{endpoint_name}
Endpoint genérico que permite criar novos endpoints dinamicamente.

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/salvar/pedidos \
  -H "Content-Type: application/json" \
  -d '{"produto": "Notebook", "quantidade": 2, "valor": 2500.00}'
```

**Resposta de sucesso:**
```json
{
  "sucesso": true,
  "id": 1,
  "endpoint": "pedidos",
  "mensagem": "Dados gravados no endpoint pedidos com sucesso"
}
```

#### GET /api/listar/{endpoint_name}
Lista todos os dados de um endpoint específico.

**Exemplo de requisição:**
```bash
curl http://localhost:5000/api/listar/pedidos
```

**Resposta:**
```json
{
  "endpoint": "pedidos",
  "total": 1,
  "dados": [
    {
      "id": 1,
      "endpoint": "pedidos",
      "content": {"produto": "Notebook", "quantidade": 2, "valor": 2500.00},
      "created_at": "2025-01-01T10:00:00"
    }
  ]
}
```

### 3. Endpoints de Consulta

#### GET /api/todos-dados
Lista todos os dados de todos os endpoints, agrupados por endpoint.

**Exemplo de requisição:**
```bash
curl http://localhost:5000/api/todos-dados
```

#### GET /api/endpoints
Lista todos os endpoints que já receberam dados e a quantidade de registros em cada um.

**Exemplo de requisição:**
```bash
curl http://localhost:5000/api/endpoints
```

**Resposta:**
```json
{
  "endpoints": ["dados", "formulario", "contato", "pedidos"],
  "contagem_por_endpoint": {
    "dados": 5,
    "formulario": 3,
    "contato": 2,
    "pedidos": 1
  },
  "total_endpoints": 4
}
```

#### GET /api/dados/{id}
Obtém um registro específico por ID.

**Exemplo de requisição:**
```bash
curl http://localhost:5000/api/dados/1
```

## Formato das Respostas

### Sucesso
Todas as respostas de sucesso incluem:
- `sucesso`: true
- `id`: ID do registro criado
- `mensagem`: Mensagem de confirmação

### Erro
Todas as respostas de erro incluem:
- `erro`: Descrição do erro ocorrido

## Notas Importantes

1. **Flexibilidade**: A API aceita qualquer estrutura JSON válida
2. **Timestamps**: Todos os registros incluem timestamp de criação automático
3. **CORS**: A API está configurada para aceitar requisições de qualquer origem
4. **Endpoints Dinâmicos**: Você pode criar novos endpoints usando `/api/salvar/{nome_do_endpoint}`
5. **Persistência**: Todos os dados são armazenados em banco SQLite local

