from src.models.user import db

class Entregador(db.Model):
    __tablename__ = 'entregador'
    
    id = db.Column(db.Integer, primary_key=True)
    email_base = db.Column(db.String(255))
    nome = db.Column(db.String(255), nullable=False)        # obrigatório
    telefone = db.Column(db.String(20), nullable=False)     # obrigatório

    def __repr__(self):
        return f'<Entregador {self.id} - {self.nome}>'

    def to_dict(self):
        return {
            'id': self.id,
            'email_base': self.email_base,
            'nome': self.nome,
            'telefone': self.telefone
        }

