# Configuração do Banco de Dados

## Configuração Atual (SQLite)

Por padrão, a API está configurada para usar SQLite, que é um banco de dados local em arquivo. A configuração atual está no arquivo `src/main.py`:

```python
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(os.path.dirname(__file__), 'database', 'app.db')}"
```

**Localização do banco:** `src/database/app.db`

## Como Alterar para Outros Bancos de Dados

### 1. PostgreSQL

Para usar PostgreSQL, altere a linha no arquivo `src/main.py`:

```python
# Substitua pelos seus dados de conexão
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://usuario:senha@localhost:5432/nome_do_banco'
```

**Exemplo completo:**
```python
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://meuusuario:minhasenha@localhost:5432/minha_api'
```

**Dependências necessárias:**
```bash
pip install psycopg2-binary
```

### 2. MySQL

Para usar MySQL, altere a linha no arquivo `src/main.py`:

```python
# Substitua pelos seus dados de conexão
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql://usuario:senha@localhost:3306/nome_do_banco'
```

**Exemplo completo:**
```python
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql://meuusuario:minhasenha@localhost:3306/minha_api'
```

**Dependências necessárias:**
```bash
pip install PyMySQL
```

### 3. SQL Server

Para usar SQL Server, altere a linha no arquivo `src/main.py`:

```python
# Substitua pelos seus dados de conexão
app.config['SQLALCHEMY_DATABASE_URI'] = 'mssql+pyodbc://usuario:senha@servidor/banco?driver=ODBC+Driver+17+for+SQL+Server'
```

**Dependências necessárias:**
```bash
pip install pyodbc
```

## Variáveis de Ambiente (Recomendado)

Para maior segurança, use variáveis de ambiente para armazenar as credenciais do banco:

### 1. Crie um arquivo `.env` na raiz do projeto:

```env
DATABASE_URL=postgresql://usuario:senha@localhost:5432/nome_do_banco
```

### 2. Instale python-dotenv:

```bash
pip install python-dotenv
```

### 3. Modifique o arquivo `src/main.py`:

```python
import os
from dotenv import load_dotenv

# Carrega variáveis do arquivo .env
load_dotenv()

# Use a variável de ambiente
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///database/app.db')
```

## Estrutura do Banco de Dados

A API cria automaticamente a seguinte tabela:

### Tabela: `data`

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | INTEGER | Chave primária (auto incremento) |
| `endpoint` | VARCHAR(100) | Nome do endpoint que recebeu os dados |
| `content` | TEXT | Dados JSON armazenados como string |
| `created_at` | DATETIME | Data e hora de criação do registro |

### Tabela: `user` (exemplo do template)

| Campo | Tipo | Descrição |
|-------|------|-----------|
| `id` | INTEGER | Chave primária (auto incremento) |
| `username` | VARCHAR(80) | Nome de usuário (único) |
| `email` | VARCHAR(120) | Email do usuário (único) |

## Comandos Úteis

### Resetar o banco de dados (SQLite):
```bash
rm src/database/app.db
```

### Ver dados no SQLite:
```bash
sqlite3 src/database/app.db
.tables
SELECT * FROM data;
.quit
```

### Backup do banco SQLite:
```bash
cp src/database/app.db backup_$(date +%Y%m%d_%H%M%S).db
```

## Migrações (Avançado)

Para projetos em produção, recomenda-se usar Flask-Migrate:

### 1. Instalar Flask-Migrate:
```bash
pip install Flask-Migrate
```

### 2. Configurar no `src/main.py`:
```python
from flask_migrate import Migrate

migrate = Migrate(app, db)
```

### 3. Comandos de migração:
```bash
# Inicializar migrações
flask db init

# Criar migração
flask db migrate -m "Descrição da mudança"

# Aplicar migração
flask db upgrade
```

## Monitoramento

Para monitorar o banco de dados em produção, considere:

1. **Logs de consulta**: Ative logs do SQLAlchemy
2. **Backup automático**: Configure backups regulares
3. **Monitoramento de performance**: Use ferramentas como New Relic ou DataDog
4. **Índices**: Adicione índices nas colunas mais consultadas

## Segurança

1. **Nunca commite credenciais** no código
2. **Use variáveis de ambiente** para dados sensíveis
3. **Configure SSL/TLS** para conexões de banco em produção
4. **Limite permissões** do usuário do banco de dados
5. **Mantenha backups** regulares e testados

