# Como Usar a API Simples

## 🚀 Início Rápido

### 1. Executar a API
```bash
cd api-simples
source venv/bin/activate
python src/main.py
```

A API estará disponível em: `http://localhost:5000`

### 2. Testar Rapidamente
```bash
# Enviar dados
curl -X POST http://localhost:5000/api/dados \
  -H "Content-Type: application/json" \
  -d '{"nome": "João", "idade": 30}'

# Ver dados gravados
curl http://localhost:5000/api/dados
```

## 📋 Endpoints Criados

| Endpoint | Método | Descrição |
|----------|--------|-----------|
| `/api/dados` | POST | Grava dados genéricos |
| `/api/dados` | GET | Lista dados genéricos |
| `/api/formulario` | POST | Grava dados de formulário |
| `/api/formulario` | GET | Lista formulários |
| `/api/contato` | POST | Grava dados de contato |
| `/api/contato` | GET | Lista contatos |
| `/api/salvar/{nome}` | POST | Grava em endpoint personalizado |
| `/api/listar/{nome}` | GET | Lista dados de endpoint específico |
| `/api/todos-dados` | GET | Lista todos os dados |
| `/api/endpoints` | GET | Lista endpoints disponíveis |

## 🗄️ Configuração do Banco de Dados

### Localização Atual
- **Arquivo**: `src/database/app.db` (SQLite)
- **Configuração**: `src/main.py` linha 23

### Para Mudar o Banco
Edite a linha no arquivo `src/main.py`:

```python
# PostgreSQL
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://usuario:senha@localhost:5432/banco'

# MySQL  
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql://usuario:senha@localhost:3306/banco'
```

**Veja mais detalhes em**: `CONFIGURACAO_BANCO.md`

## 📁 Arquivos Importantes

- **`ENDPOINTS.md`** - Documentação completa dos endpoints
- **`CONFIGURACAO_BANCO.md`** - Como configurar diferentes bancos
- **`README.md`** - Documentação geral do projeto
- **`src/main.py`** - Arquivo principal (onde você configura o banco)
- **`src/routes/data.py`** - Onde estão definidos os endpoints
- **`src/models/data.py`** - Modelo do banco de dados

## 🧪 Testar a API

Execute o script de teste incluído:
```bash
python test_models.py
```

## 💡 Exemplos Práticos

### Criar endpoint personalizado para pedidos:
```bash
curl -X POST http://localhost:5000/api/salvar/pedidos \
  -H "Content-Type: application/json" \
  -d '{
    "produto": "Notebook",
    "quantidade": 1,
    "valor": 2500.00
  }'
```

### Listar pedidos:
```bash
curl http://localhost:5000/api/listar/pedidos
```

### Ver todos os endpoints criados:
```bash
curl http://localhost:5000/api/endpoints
```

## ⚙️ Características

- ✅ **Flexível**: Aceita qualquer JSON
- ✅ **Endpoints Dinâmicos**: Crie novos automaticamente
- ✅ **CORS Habilitado**: Funciona com qualquer frontend
- ✅ **Timestamps**: Data/hora automática
- ✅ **Banco Configurável**: SQLite, PostgreSQL, MySQL, etc.

## 🔧 Solução de Problemas

1. **Porta ocupada**: Mude a porta no `src/main.py` (linha 40)
2. **Erro de banco**: Verifique `CONFIGURACAO_BANCO.md`
3. **CORS**: Já está habilitado por padrão
4. **Dependências**: Execute `pip install -r requirements.txt`

