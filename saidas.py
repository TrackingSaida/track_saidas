from src.models.user import db
from datetime import datetime

class Saidas(db.Model):
    __tablename__ = 'saidas'
    
    id_saida = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.Date, nullable=False)
    base = db.Column(db.String(255))
    entregador = db.Column(db.String(255), nullable=False)  # obrigatório
    codigo = db.Column(db.String(50), nullable=False)       # obrigatório
    servico = db.Column(db.String(100))
    status = db.Column(db.String(50))
    estacao = db.Column(db.Integer, nullable=False)         # obrigatório

    def __repr__(self):
        return f'<Saidas {self.id_saida} - {self.codigo}>'

    def to_dict(self):
        return {
            'id_saida': self.id_saida,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'data': self.data.isoformat() if self.data else None,
            'base': self.base,
            'entregador': self.entregador,
            'codigo': self.codigo,
            'servico': self.servico,
            'status': self.status,
            'estacao': self.estacao
        }

