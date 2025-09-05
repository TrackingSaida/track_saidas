from src.models.user import db

class Estacao(db.Model):
    __tablename__ = 'estacao'
    
    id = db.Column(db.Integer, primary_key=True)
    email_base = db.Column(db.String(255))
    estacao = db.Column(db.Integer, nullable=False)         # obrigatório

    def __repr__(self):
        return f'<Estacao {self.id} - {self.estacao}>'

    def to_dict(self):
        return {
            'id': self.id,
            'email_base': self.email_base,
            'estacao': self.estacao
        }

