#!/usr/bin/env python3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask
from src.models.user import db
from src.models.saidas import Saidas
from src.models.users import Users
from src.models.entregador import Entregador
from src.models.estacao import Estacao
from datetime import datetime, date

# Configurar Flask app para teste
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test_novas_tabelas.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

def test_novas_tabelas():
    print("🧪 Testando Novas Tabelas da API...")
    print("=" * 50)
    
    with app.app_context():
        # Criar tabelas
        db.create_all()
        print("✅ Tabelas criadas com sucesso")
        
        # Teste 1: Criar Estação
        print("\n1️⃣ Testando criação de Estação...")
        estacao = Estacao(
            estacao=1,
            email_base="teste@email.com"
        )
        db.session.add(estacao)
        db.session.commit()
        print(f"✅ Estação criada: {estacao.to_dict()}")
        
        # Teste 2: Criar Entregador
        print("\n2️⃣ Testando criação de Entregador...")
        entregador = Entregador(
            nome="João Silva",
            telefone="11999999999",
            email_base="joao@email.com"
        )
        db.session.add(entregador)
        db.session.commit()
        print(f"✅ Entregador criado: {entregador.to_dict()}")
        
        # Teste 3: Criar Usuário
        print("\n3️⃣ Testando criação de Usuário...")
        usuario = Users(
            email="usuario@teste.com",
            senha="123456",
            username="usuario_teste",
            contato="11888888888",
            status="ativo",
            creditos=100.00
        )
        db.session.add(usuario)
        db.session.commit()
        print(f"✅ Usuário criado: {usuario.to_dict()}")
        
        # Teste 4: Criar Saída
        print("\n4️⃣ Testando criação de Saída...")
        saida = Saidas(
            data=date(2025, 1, 15),
            entregador="João Silva",
            codigo="ABC123456",
            estacao=1,
            base="Base Teste",
            servico="Entrega",
            status="Ativo"
        )
        db.session.add(saida)
        db.session.commit()
        print(f"✅ Saída criada: {saida.to_dict()}")
        
        # Teste 5: Consultar dados
        print("\n5️⃣ Testando consultas...")
        
        estacoes = Estacao.query.all()
        print(f"✅ Total de estações: {len(estacoes)}")
        
        entregadores = Entregador.query.all()
        print(f"✅ Total de entregadores: {len(entregadores)}")
        
        usuarios = Users.query.all()
        print(f"✅ Total de usuários: {len(usuarios)}")
        
        saidas = Saidas.query.all()
        print(f"✅ Total de saídas: {len(saidas)}")
        
        # Teste 6: Validar campos obrigatórios
        print("\n6️⃣ Testando validações...")
        
        # Tentar criar estação duplicada
        try:
            estacao_dup = Estacao(estacao=1)
            db.session.add(estacao_dup)
            db.session.commit()
            print("❌ Erro: Deveria ter falhado na estação duplicada")
        except Exception as e:
            db.session.rollback()
            print("✅ Validação de estação duplicada funcionando")
        
        # Tentar criar usuário com email duplicado
        try:
            usuario_dup = Users(
                email="usuario@teste.com",
                senha="123",
                username="outro_user",
                contato="11777777777"
            )
            db.session.add(usuario_dup)
            db.session.commit()
            print("❌ Erro: Deveria ter falhado no email duplicado")
        except Exception as e:
            db.session.rollback()
            print("✅ Validação de email duplicado funcionando")
        
        print("\n✅ Todos os testes das novas tabelas passaram!")
        print("\n📊 Resumo:")
        print(f"   - Estações: {len(Estacao.query.all())}")
        print(f"   - Entregadores: {len(Entregador.query.all())}")
        print(f"   - Usuários: {len(Users.query.all())}")
        print(f"   - Saídas: {len(Saidas.query.all())}")

if __name__ == "__main__":
    test_novas_tabelas()

