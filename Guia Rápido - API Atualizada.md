# Guia Rápido - API Atualizada

## 🚀 Como Usar as Novas Tabelas

### 1. Executar a API
```bash
cd api-simples
source venv/bin/activate
python src/main.py
```

### 2. Testar as Novas Tabelas

#### ✅ Criar uma Saída
```bash
curl -X POST http://localhost:5000/api/saidas \
  -H "Content-Type: application/json" \
  -d '{
    "data": "2025-01-15",
    "entregador": "Adriel Caue",
    "codigo": "1239123810",
    "estacao": 1
  }'
```

#### ✅ Criar um Usuário
```bash
curl -X POST http://localhost:5000/api/users \
  -H "Content-Type: application/json" \
  -d '{
    "email": "teste@email.com",
    "senha": "123456",
    "username": "usuario_teste",
    "contato": "11999999999"
  }'
```

#### ✅ Criar um Entregador
```bash
curl -X POST http://localhost:5000/api/entregador \
  -H "Content-Type: application/json" \
  -d '{
    "nome": "João Silva",
    "telefone": "11888888888"
  }'
```

#### ✅ Criar uma Estação
```bash
curl -X POST http://localhost:5000/api/estacao \
  -H "Content-Type: application/json" \
  -d '{
    "estacao": 2
  }'
```

### 3. Listar Dados

```bash
# Listar saídas
curl http://localhost:5000/api/saidas

# Listar usuários
curl http://localhost:5000/api/users

# Listar entregadores
curl http://localhost:5000/api/entregador

# Listar estações
curl http://localhost:5000/api/estacao
```

## 📋 Campos Obrigatórios

| Tabela | Campos Obrigatórios |
|--------|-------------------|
| **saidas** | `data`, `entregador`, `codigo`, `estacao` |
| **users** | `email`, `senha`, `username`, `contato` |
| **entregador** | `nome`, `telefone` |
| **estacao** | `estacao` |

## 🗄️ Estrutura do Banco

A API criará automaticamente as seguintes tabelas:

### Tabela: saidas
- `id_saida` (PK, auto)
- `timestamp` (auto)
- `data` ⚠️ **obrigatório**
- `base`
- `entregador` ⚠️ **obrigatório**
- `codigo` ⚠️ **obrigatório**
- `servico`
- `status`
- `estacao` ⚠️ **obrigatório**

### Tabela: users
- `id` (PK, auto)
- `email` ⚠️ **obrigatório, único**
- `senha` ⚠️ **obrigatório**
- `username` ⚠️ **obrigatório, único**
- `contato` ⚠️ **obrigatório**
- `status`
- `cobranca`
- `valor_r`
- `mensalidade`
- `creditos`

### Tabela: entregador
- `id` (PK, auto)
- `email_base`
- `nome` ⚠️ **obrigatório**
- `telefone` ⚠️ **obrigatório**

### Tabela: estacao
- `id` (PK, auto)
- `email_base`
- `estacao` ⚠️ **obrigatório, único**

## 🔧 Configuração do Banco

**Localização atual:** `src/database/app.db` (SQLite)

**Para mudar para seu banco:** Edite a linha 35 do arquivo `src/main.py`:

```python
# PostgreSQL
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://usuario:senha@localhost:5432/seu_banco'

# MySQL
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql://usuario:senha@localhost:3306/seu_banco'
```

## 📁 Arquivos Importantes

- **`ENDPOINTS_ATUALIZADOS.md`** - Documentação completa dos novos endpoints
- **`src/main.py`** - Arquivo principal (configure seu banco aqui)
- **`src/models/`** - Modelos das tabelas (saidas.py, users.py, entregador.py, estacao.py)
- **`src/routes/`** - Rotas da API para cada tabela

## ⚡ Exemplos Práticos

### Fluxo Completo de Teste:

```bash
# 1. Criar estação
curl -X POST http://localhost:5000/api/estacao \
  -H "Content-Type: application/json" \
  -d '{"estacao": 1}'

# 2. Criar entregador
curl -X POST http://localhost:5000/api/entregador \
  -H "Content-Type: application/json" \
  -d '{"nome": "João", "telefone": "11999999999"}'

# 3. Criar usuário
curl -X POST http://localhost:5000/api/users \
  -H "Content-Type: application/json" \
  -d '{"email": "joao@email.com", "senha": "123", "username": "joao", "contato": "11999999999"}'

# 4. Criar saída
curl -X POST http://localhost:5000/api/saidas \
  -H "Content-Type: application/json" \
  -d '{"data": "2025-01-15", "entregador": "João", "codigo": "ABC123", "estacao": 1}'
```

## 🎯 Próximos Passos

1. **Configure seu banco de dados** no `src/main.py`
2. **Teste todos os endpoints** com seus dados reais
3. **Integre com seu frontend** usando os endpoints documentados
4. **Monitore os logs** para verificar se tudo está funcionando

A API está pronta para trabalhar com suas 4 tabelas principais! 🚀

