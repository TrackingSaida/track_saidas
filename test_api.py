#!/usr/bin/env python3
import requests
import json
import time

def test_api():
    base_url = "http://localhost:5001/api"
    
    print("🧪 Testando API Simples...")
    print("=" * 50)
    
    # Aguardar um pouco para garantir que o servidor está rodando
    time.sleep(2)
    
    try:
        # Teste 1: Listar endpoints (deve estar vazio inicialmente)
        print("\n1️⃣ Testando GET /api/endpoints")
        response = requests.get(f"{base_url}/endpoints", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        # Teste 2: Enviar dados genéricos
        print("\n2️⃣ Testando POST /api/dados")
        dados_teste = {
            "nome": "João Silva",
            "idade": 30,
            "cidade": "São Paulo",
            "teste": True
        }
        response = requests.post(f"{base_url}/dados", json=dados_teste, timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        # Teste 3: Listar dados gravados
        print("\n3️⃣ Testando GET /api/dados")
        response = requests.get(f"{base_url}/dados", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        # Teste 4: Endpoint dinâmico
        print("\n4️⃣ Testando POST /api/salvar/pedidos")
        pedido_teste = {
            "produto": "Notebook Dell",
            "quantidade": 1,
            "valor": 2500.00,
            "cliente": "Maria Santos"
        }
        response = requests.post(f"{base_url}/salvar/pedidos", json=pedido_teste, timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        # Teste 5: Listar pedidos
        print("\n5️⃣ Testando GET /api/listar/pedidos")
        response = requests.get(f"{base_url}/listar/pedidos", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        # Teste 6: Listar todos os endpoints após inserções
        print("\n6️⃣ Testando GET /api/endpoints (após inserções)")
        response = requests.get(f"{base_url}/endpoints", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        # Teste 7: Todos os dados
        print("\n7️⃣ Testando GET /api/todos-dados")
        response = requests.get(f"{base_url}/todos-dados", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.json()}")
        
        print("\n✅ Todos os testes concluídos com sucesso!")
        
    except requests.exceptions.ConnectionError:
        print("❌ Erro: Não foi possível conectar à API. Verifique se ela está rodando.")
    except requests.exceptions.Timeout:
        print("❌ Erro: Timeout na requisição. A API pode estar lenta ou não respondendo.")
    except Exception as e:
        print(f"❌ Erro inesperado: {e}")

if __name__ == "__main__":
    test_api()

