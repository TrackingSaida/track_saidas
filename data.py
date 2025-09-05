from flask import Blueprint, jsonify, request
from src.models.data import Data, db

data_bp = Blueprint('data', __name__)

@data_bp.route('/dados', methods=['POST'])
def receber_dados():
    """
    Endpoint para receber dados genéricos
    Aceita qualquer JSON e grava no banco
    """
    try:
        dados = request.json
        if not dados:
            return jsonify({'erro': 'Nenhum dado fornecido'}), 400
        
        # Criar novo registro
        novo_registro = Data(endpoint='dados')
        novo_registro.set_content(dados)
        
        db.session.add(novo_registro)
        db.session.commit()
        
        return jsonify({
            'sucesso': True,
            'id': novo_registro.id,
            'mensagem': 'Dados gravados com sucesso'
        }), 201
        
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/dados', methods=['GET'])
def listar_dados():
    """
    Endpoint para listar todos os dados gravados
    """
    try:
        registros = Data.query.filter_by(endpoint='dados').all()
        return jsonify([registro.to_dict() for registro in registros])
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/dados/<int:data_id>', methods=['GET'])
def obter_dado(data_id):
    """
    Endpoint para obter um dado específico por ID
    """
    try:
        registro = Data.query.get_or_404(data_id)
        return jsonify(registro.to_dict())
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/formulario', methods=['POST'])
def receber_formulario():
    """
    Endpoint específico para dados de formulário
    """
    try:
        dados = request.json
        if not dados:
            return jsonify({'erro': 'Nenhum dado fornecido'}), 400
        
        # Criar novo registro
        novo_registro = Data(endpoint='formulario')
        novo_registro.set_content(dados)
        
        db.session.add(novo_registro)
        db.session.commit()
        
        return jsonify({
            'sucesso': True,
            'id': novo_registro.id,
            'mensagem': 'Formulário gravado com sucesso'
        }), 201
        
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/formulario', methods=['GET'])
def listar_formularios():
    """
    Endpoint para listar todos os formulários gravados
    """
    try:
        registros = Data.query.filter_by(endpoint='formulario').all()
        return jsonify([registro.to_dict() for registro in registros])
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/contato', methods=['POST'])
def receber_contato():
    """
    Endpoint específico para dados de contato
    """
    try:
        dados = request.json
        if not dados:
            return jsonify({'erro': 'Nenhum dado fornecido'}), 400
        
        # Criar novo registro
        novo_registro = Data(endpoint='contato')
        novo_registro.set_content(dados)
        
        db.session.add(novo_registro)
        db.session.commit()
        
        return jsonify({
            'sucesso': True,
            'id': novo_registro.id,
            'mensagem': 'Contato gravado com sucesso'
        }), 201
        
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/contato', methods=['GET'])
def listar_contatos():
    """
    Endpoint para listar todos os contatos gravados
    """
    try:
        registros = Data.query.filter_by(endpoint='contato').all()
        return jsonify([registro.to_dict() for registro in registros])
    except Exception as e:
        return jsonify({'erro': str(e)}), 500



@data_bp.route('/salvar/<endpoint_name>', methods=['POST'])
def salvar_dados_genericos(endpoint_name):
    """
    Endpoint genérico para salvar dados em qualquer endpoint
    Permite criar endpoints dinamicamente
    """
    try:
        dados = request.json
        if not dados:
            return jsonify({'erro': 'Nenhum dado fornecido'}), 400
        
        # Criar novo registro com o nome do endpoint fornecido
        novo_registro = Data(endpoint=endpoint_name)
        novo_registro.set_content(dados)
        
        db.session.add(novo_registro)
        db.session.commit()
        
        return jsonify({
            'sucesso': True,
            'id': novo_registro.id,
            'endpoint': endpoint_name,
            'mensagem': f'Dados gravados no endpoint {endpoint_name} com sucesso'
        }), 201
        
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/listar/<endpoint_name>', methods=['GET'])
def listar_dados_por_endpoint(endpoint_name):
    """
    Endpoint genérico para listar dados de um endpoint específico
    """
    try:
        registros = Data.query.filter_by(endpoint=endpoint_name).all()
        return jsonify({
            'endpoint': endpoint_name,
            'total': len(registros),
            'dados': [registro.to_dict() for registro in registros]
        })
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/todos-dados', methods=['GET'])
def listar_todos_dados():
    """
    Endpoint para listar todos os dados de todos os endpoints
    """
    try:
        registros = Data.query.all()
        
        # Agrupar por endpoint
        dados_por_endpoint = {}
        for registro in registros:
            if registro.endpoint not in dados_por_endpoint:
                dados_por_endpoint[registro.endpoint] = []
            dados_por_endpoint[registro.endpoint].append(registro.to_dict())
        
        return jsonify({
            'total_registros': len(registros),
            'endpoints': list(dados_por_endpoint.keys()),
            'dados_por_endpoint': dados_por_endpoint
        })
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

@data_bp.route('/endpoints', methods=['GET'])
def listar_endpoints():
    """
    Endpoint para listar todos os endpoints que já receberam dados
    """
    try:
        endpoints = db.session.query(Data.endpoint).distinct().all()
        endpoints_list = [endpoint[0] for endpoint in endpoints]
        
        # Contar registros por endpoint
        contagem = {}
        for endpoint in endpoints_list:
            count = Data.query.filter_by(endpoint=endpoint).count()
            contagem[endpoint] = count
        
        return jsonify({
            'endpoints': endpoints_list,
            'contagem_por_endpoint': contagem,
            'total_endpoints': len(endpoints_list)
        })
    except Exception as e:
        return jsonify({'erro': str(e)}), 500

