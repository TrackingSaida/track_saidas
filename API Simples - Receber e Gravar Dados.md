# API Simples - Receber e Gravar Dados

Uma API Flask simples e flexível para receber dados JSON via endpoints e gravar no banco de dados.

## Características

- ✅ **Simples**: Fácil de usar e configurar
- ✅ **Flexível**: Aceita qualquer estrutura JSON
- ✅ **Endpoints Dinâmicos**: Crie novos endpoints automaticamente
- ✅ **CORS Habilitado**: Funciona com qualquer frontend
- ✅ **Banco Configurável**: SQLite, PostgreSQL, MySQL, SQL Server
- ✅ **Timestamps Automáticos**: Registra data/hora de criação

## Início Rápido

### 1. Instalar Dependências
```bash
cd api-simples
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Executar a API
```bash
python src/main.py
```

A API estará disponível em: `http://localhost:5000`

### 3. Testar um Endpoint
```bash
curl -X POST http://localhost:5000/api/dados \
  -H "Content-Type: application/json" \
  -d '{"nome": "João", "idade": 30}'
```

## Arquivos Importantes

- **`ENDPOINTS.md`** - Documentação completa de todos os endpoints disponíveis
- **`CONFIGURACAO_BANCO.md`** - Instruções para configurar diferentes bancos de dados
- **`src/main.py`** - Arquivo principal da aplicação
- **`src/routes/data.py`** - Definição dos endpoints de dados
- **`src/models/data.py`** - Modelo do banco de dados
- **`requirements.txt`** - Dependências do projeto

## Estrutura do Projeto

```
api-simples/
├── src/
│   ├── models/
│   │   ├── user.py          # Modelo de usuário (exemplo)
│   │   └── data.py          # Modelo de dados principal
│   ├── routes/
│   │   ├── user.py          # Rotas de usuário (exemplo)
│   │   └── data.py          # Rotas de dados principais
│   ├── static/              # Arquivos estáticos
│   ├── database/
│   │   └── app.db          # Banco SQLite
│   └── main.py             # Aplicação principal
├── venv/                   # Ambiente virtual
├── ENDPOINTS.md           # Documentação dos endpoints
├── CONFIGURACAO_BANCO.md  # Configuração do banco
├── requirements.txt       # Dependências
└── README.md             # Este arquivo
```

## Endpoints Principais

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| POST | `/api/dados` | Grava dados genéricos |
| GET | `/api/dados` | Lista dados genéricos |
| POST | `/api/formulario` | Grava dados de formulário |
| GET | `/api/formulario` | Lista formulários |
| POST | `/api/contato` | Grava dados de contato |
| GET | `/api/contato` | Lista contatos |
| POST | `/api/salvar/{nome}` | Grava dados em endpoint personalizado |
| GET | `/api/listar/{nome}` | Lista dados de endpoint específico |
| GET | `/api/todos-dados` | Lista todos os dados |
| GET | `/api/endpoints` | Lista endpoints disponíveis |

## Configuração do Banco de Dados

### SQLite (Padrão)
Não requer configuração adicional. O banco é criado automaticamente em `src/database/app.db`.

### Outros Bancos
Consulte o arquivo `CONFIGURACAO_BANCO.md` para instruções detalhadas sobre PostgreSQL, MySQL e SQL Server.

## Exemplos de Uso

### Salvar dados de um formulário de contato:
```bash
curl -X POST http://localhost:5000/api/contato \
  -H "Content-Type: application/json" \
  -d '{
    "nome": "Maria Silva",
    "email": "maria@email.com",
    "telefone": "11999999999",
    "mensagem": "Gostaria de mais informações"
  }'
```

### Criar um endpoint personalizado para pedidos:
```bash
curl -X POST http://localhost:5000/api/salvar/pedidos \
  -H "Content-Type: application/json" \
  -d '{
    "produto": "Notebook Dell",
    "quantidade": 1,
    "valor": 2500.00,
    "cliente": "João Santos"
  }'
```

### Listar todos os pedidos:
```bash
curl http://localhost:5000/api/listar/pedidos
```

## Deployment

### Desenvolvimento Local
```bash
python src/main.py
```

### Produção
Para produção, use um servidor WSGI como Gunicorn:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 src.main:app
```

## Segurança

- Configure variáveis de ambiente para credenciais do banco
- Use HTTPS em produção
- Implemente autenticação se necessário
- Configure rate limiting para evitar spam

## Suporte

Para dúvidas ou problemas:
1. Consulte a documentação em `ENDPOINTS.md`
2. Verifique a configuração do banco em `CONFIGURACAO_BANCO.md`
3. Verifique os logs da aplicação

## Licença

Este projeto é fornecido como exemplo e pode ser modificado conforme necessário.

