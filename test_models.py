#!/usr/bin/env python3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask
from src.models.user import db
from src.models.data import Data

# Configurar Flask app para teste
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

def test_models():
    print("🧪 Testando modelos da API...")
    print("=" * 40)
    
    with app.app_context():
        # Criar tabelas
        db.create_all()
        print("✅ Tabelas criadas com sucesso")
        
        # Testar criação de dados
        print("\n📝 Testando criação de dados...")
        
        # Criar um registro de teste
        dados_teste = {
            "nome": "João Silva",
            "idade": 30,
            "cidade": "São Paulo"
        }
        
        novo_registro = Data(endpoint='dados')
        novo_registro.set_content(dados_teste)
        
        db.session.add(novo_registro)
        db.session.commit()
        
        print(f"✅ Registro criado com ID: {novo_registro.id}")
        
        # Testar recuperação de dados
        print("\n📖 Testando recuperação de dados...")
        
        registros = Data.query.filter_by(endpoint='dados').all()
        print(f"✅ Encontrados {len(registros)} registros")
        
        for registro in registros:
            print(f"   - ID: {registro.id}")
            print(f"   - Endpoint: {registro.endpoint}")
            print(f"   - Conteúdo: {registro.get_content()}")
            print(f"   - Criado em: {registro.created_at}")
        
        # Testar to_dict
        print("\n🔄 Testando conversão para dict...")
        dict_data = novo_registro.to_dict()
        print(f"✅ Dict: {dict_data}")
        
        print("\n✅ Todos os testes dos modelos passaram!")

if __name__ == "__main__":
    test_models()

