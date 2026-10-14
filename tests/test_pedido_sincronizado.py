from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from models import Saida
from pedido_sincronizado import (
    SQL_SINCRONIZADO_NAO_COLETADO,
    aguardando_coleta,
    sincronizado_nao_coletado,
)


def _db():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE saidas (id_saida INTEGER PRIMARY KEY, id_coleta INTEGER)"))
        conn.execute(text("CREATE TABLE saida_historico (id INTEGER PRIMARY KEY, id_saida INTEGER, evento TEXT)"))
        # 1: ML sincronizado nunca coletado; 2: Shopee sincronizado coletado;
        # 3: Shopee sincronizado nunca coletado; 4: pacote lido no scanner, sem coleta.
        conn.execute(text("INSERT INTO saidas VALUES (1, NULL), (2, 50), (3, NULL), (4, NULL)"))
        conn.execute(text(
            "INSERT INTO saida_historico (id_saida, evento) VALUES "
            "(1, 'ml_int_sync'), (2, 'shopee_int_sync'), (2, 'criado_coleta'), "
            "(3, 'shopee_int_sync'), (4, 'lido')"
        ))
    return engine


def test_filtro_orm_identifica_sincronizado_nunca_coletado():
    with Session(_db()) as db:
        ids = db.scalars(select(Saida.id_saida).where(sincronizado_nao_coletado())).all()
        fora = db.scalars(select(Saida.id_saida).where(~sincronizado_nao_coletado())).all()
    assert sorted(ids) == [1, 3]
    assert sorted(fora) == [2, 4]


def test_fragmento_sql_bate_com_filtro_orm():
    with _db().connect() as conn:
        ids = conn.execute(text(f"SELECT s.id_saida FROM saidas s WHERE {SQL_SINCRONIZADO_NAO_COLETADO}")).scalars().all()
    assert sorted(ids) == [1, 3]


def test_aguardando_coleta():
    assert aguardando_coleta(" Aguardando_Coleta ")
    assert not aguardando_coleta("coletado")
    assert not aguardando_coleta(None)


def test_apuracao_owner_exclui_sincronizado_nao_coletado():
    import owner_cobranca_service

    assert "AND NOT " + SQL_SINCRONIZADO_NAO_COLETADO in str(owner_cobranca_service._SQL_EVENTOS)
