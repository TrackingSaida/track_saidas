from src.models.user import db
from datetime import datetime

class Users(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False)       # obrigatório
    senha = db.Column(db.String(255), nullable=False)       # obrigatório
    username = db.Column(db.String(100), nullable=False)    # obrigatório
    contato = db.Column(db.String(20), nullable=False)      # obrigatório
    status = db.Column(db.String(50))
    cobranca = db.Column(db.String(50))
    valor_r = db.Column(db.Numeric(10, 2))  # R$
    mensalidade = db.Column(db.Date)
    creditos = db.Column(db.Numeric(10, 2))

    def __repr__(self):
        return f'<Users {self.id} - {self.username}>'

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'senha': self.senha,
            'username': self.username,
            'contato': self.contato,
            'status': self.status,
            'cobranca': self.cobranca,
            'valor_r': float(self.valor_r) if self.valor_r else None,
            'mensalidade': self.mensalidade.isoformat() if self.mensalidade else None,
            'creditos': float(self.creditos) if self.creditos else None
        }

