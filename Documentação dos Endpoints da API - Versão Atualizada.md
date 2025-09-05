# Documentação dos Endpoints da API - Versão Atualizada

Esta API permite interagir com as tabelas do banco de dados: **saidas**, **users**, **entregador** e **estacao**.

## Base URL
```
http://localhost:5000/api
```

## 🚀 Novos Endpoints das Tabelas Principais

### 1. Endpoints da Tabela SAIDAS

#### POST /api/saidas
Criar nova saída.

**Campos obrigatórios:** `data`, `entregador`, `codigo`, `estacao`

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/saidas \
  -H "Content-Type: application/json" \
  -d '{
    "data": "2025-01-15",
    "entregador": "Adriel Caue",
    "codigo": "1239123810",
    "estacao": 1,
    "base": "chrigor.henrique...",
    "servico": "abcde",
    "status": "satu"
  }'
```

**Resposta de sucesso:**
```json
{
  "sucesso": true,
  "id_saida": 1,
  "mensagem": "Saída criada com sucesso",
  "dados": {
    "id_saida": 1,
    "timestamp": "2025-01-15T10:30:00",
    "data": "2025-01-15",
    "base": "chrigor.henrique...",
    "entregador": "Adriel Caue",
    "codigo": "1239123810",
    "servico": "abcde",
    "status": "satu",
    "estacao": 1
  }
}
```

#### GET /api/saidas
Listar todas as saídas.

#### GET /api/saidas/{id}
Obter saída específica por ID.

#### PUT /api/saidas/{id}
Atualizar saída existente.

#### DELETE /api/saidas/{id}
Deletar saída.

---

### 2. Endpoints da Tabela USERS

#### POST /api/users
Criar novo usuário.

**Campos obrigatórios:** `email`, `senha`, `username`, `contato`

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/users \
  -H "Content-Type: application/json" \
  -d '{
    "email": "cristian.mello@email.com",
    "senha": "minhasenha123",
    "username": "cristian.mello",
    "contato": "213134565",
    "status": "ativo",
    "cobranca": "valor",
    "valor_r": 0.01,
    "mensalidade": "2025-10-01",
    "creditos": 30.00
  }'
```

**Resposta de sucesso:**
```json
{
  "sucesso": true,
  "id": 1,
  "mensagem": "Usuário criado com sucesso",
  "dados": {
    "id": 1,
    "email": "cristian.mello@email.com",
    "senha": "minhasenha123",
    "username": "cristian.mello",
    "contato": "213134565",
    "status": "ativo",
    "cobranca": "valor",
    "valor_r": 0.01,
    "mensalidade": "2025-10-01",
    "creditos": 30.00
  }
}
```

#### GET /api/users
Listar todos os usuários.

#### GET /api/users/{id}
Obter usuário específico por ID.

#### PUT /api/users/{id}
Atualizar usuário existente.

#### DELETE /api/users/{id}
Deletar usuário.

---

### 3. Endpoints da Tabela ENTREGADOR

#### POST /api/entregador
Criar novo entregador.

**Campos obrigatórios:** `nome`, `telefone`

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/entregador \
  -H "Content-Type: application/json" \
  -d '{
    "nome": "Adriel Caue",
    "telefone": "1111111115",
    "email_base": "chrigor.henrique..."
  }'
```

**Resposta de sucesso:**
```json
{
  "sucesso": true,
  "id": 1,
  "mensagem": "Entregador criado com sucesso",
  "dados": {
    "id": 1,
    "email_base": "chrigor.henrique...",
    "nome": "Adriel Caue",
    "telefone": "1111111115"
  }
}
```

#### GET /api/entregador
Listar todos os entregadores.

#### GET /api/entregador/{id}
Obter entregador específico por ID.

#### PUT /api/entregador/{id}
Atualizar entregador existente.

#### DELETE /api/entregador/{id}
Deletar entregador.

---

### 4. Endpoints da Tabela ESTACAO

#### POST /api/estacao
Criar nova estação.

**Campos obrigatórios:** `estacao`

**Exemplo de requisição:**
```bash
curl -X POST http://localhost:5000/api/estacao \
  -H "Content-Type: application/json" \
  -d '{
    "estacao": 1,
    "email_base": "chrigor.henrique..."
  }'
```

**Resposta de sucesso:**
```json
{
  "sucesso": true,
  "id": 1,
  "mensagem": "Estação criada com sucesso",
  "dados": {
    "id": 1,
    "email_base": "chrigor.henrique...",
    "estacao": 1
  }
}
```

#### GET /api/estacao
Listar todas as estações.

#### GET /api/estacao/{id}
Obter estação específica por ID.

#### PUT /api/estacao/{id}
Atualizar estação existente.

#### DELETE /api/estacao/{id}
Deletar estação.

---

## 📋 Resumo dos Endpoints

| Tabela | Método | Endpoint | Descrição |
|--------|--------|----------|-----------|
| **saidas** | POST | `/api/saidas` | Criar saída |
| **saidas** | GET | `/api/saidas` | Listar saídas |
| **saidas** | GET | `/api/saidas/{id}` | Obter saída |
| **saidas** | PUT | `/api/saidas/{id}` | Atualizar saída |
| **saidas** | DELETE | `/api/saidas/{id}` | Deletar saída |
| **users** | POST | `/api/users` | Criar usuário |
| **users** | GET | `/api/users` | Listar usuários |
| **users** | GET | `/api/users/{id}` | Obter usuário |
| **users** | PUT | `/api/users/{id}` | Atualizar usuário |
| **users** | DELETE | `/api/users/{id}` | Deletar usuário |
| **entregador** | POST | `/api/entregador` | Criar entregador |
| **entregador** | GET | `/api/entregador` | Listar entregadores |
| **entregador** | GET | `/api/entregador/{id}` | Obter entregador |
| **entregador** | PUT | `/api/entregador/{id}` | Atualizar entregador |
| **entregador** | DELETE | `/api/entregador/{id}` | Deletar entregador |
| **estacao** | POST | `/api/estacao` | Criar estação |
| **estacao** | GET | `/api/estacao` | Listar estações |
| **estacao** | GET | `/api/estacao/{id}` | Obter estação |
| **estacao** | PUT | `/api/estacao/{id}` | Atualizar estação |
| **estacao** | DELETE | `/api/estacao/{id}` | Deletar estação |

## 🔴 Campos Obrigatórios por Tabela

### Tabela SAIDAS
- `data` (formato: YYYY-MM-DD)
- `entregador` (string)
- `codigo` (string)
- `estacao` (integer)

### Tabela USERS
- `email` (string, único)
- `senha` (string)
- `username` (string, único)
- `contato` (string)

### Tabela ENTREGADOR
- `nome` (string)
- `telefone` (string)

### Tabela ESTACAO
- `estacao` (integer, único)

## 📝 Notas Importantes

1. **Validação**: Todos os campos obrigatórios são validados
2. **Unicidade**: Emails, usernames e estações devem ser únicos
3. **Formato de Data**: Use sempre YYYY-MM-DD para datas
4. **CORS**: Habilitado para todas as rotas
5. **Rollback**: Erros fazem rollback automático das transações
6. **Timestamps**: Saídas incluem timestamp automático de criação

## 🚫 Endpoints Antigos (Ainda Disponíveis)

Os endpoints genéricos anteriores ainda funcionam:
- `/api/dados`, `/api/formulario`, `/api/contato`
- `/api/salvar/{nome}`, `/api/listar/{nome}`
- `/api/todos-dados`, `/api/endpoints`

