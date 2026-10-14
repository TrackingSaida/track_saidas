from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from coleta_a_coletar_service import (
    a_coletar_item,
    chave_base,
    contagem_a_coletar,
    contas_por_base,
    resumo_a_coletar,
)
from models import MLConexao, Saida, ShopeeToken


def _criar(conn, model):
    cols = ", ".join(
        f"{c.name} INTEGER PRIMARY KEY" if c.primary_key else c.name for c in model.__table__.columns
    )
    conn.execute(text(f"CREATE TABLE {model.__tablename__} ({cols})"))


def _db():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for model in (Saida, MLConexao, ShopeeToken):
            _criar(conn, model)
        conn.execute(text(
            "INSERT INTO saidas (id_saida, sub_base, base, status, ml_shipment_id, shopee_order_sn) VALUES "
            "(1, 'SB1', 'JRP', 'aguardando_coleta', 111, NULL), "
            "(2, 'SB1', ' jrp ', 'aguardando_coleta', 112, NULL), "
            "(3, 'SB1', 'MBI', 'aguardando_coleta', NULL, 'SN1'), "
            "(4, 'SB1', 'MBI', 'coletado', 113, NULL), "
            "(5, 'SB2', 'JRP', 'aguardando_coleta', 114, NULL)"
        ))
        conn.execute(text(
            "INSERT INTO ml_conexoes (id, sub_base, user_id_ml, user_nickname_ml, id_base, criado_em) VALUES "
            "(1, 'SB1', 10, 'JSSICA', 225, '2026-10-01'), "
            "(2, 'SB1', 20, 'SEMVINCULO', NULL, '2026-10-01'), "
            "(3, 'SB2', 30, 'OUTRO_TENANT', 225, '2026-10-01')"
        ))
        conn.execute(text(
            "INSERT INTO shopee_tokens (id, shop_id, sub_base, shop_name, id_base, criado_em) VALUES "
            "(1, 500, 'SB1', 'loja antiga', 97, '2026-09-01'), "
            "(2, 500, 'SB1', 'mbi.store', 97, '2026-10-01')"
        ))
    return engine


def test_contagem_por_base_respeita_sub_base_e_status():
    with Session(_db()) as db:
        assert contagem_a_coletar(db, "SB1") == {
            "JRP": {"mercado_livre": 2, "shopee": 0},
            "MBI": {"mercado_livre": 0, "shopee": 1},
        }
        assert contagem_a_coletar(db, "SB2") == {"JRP": {"mercado_livre": 1, "shopee": 0}}


def test_contas_vinculadas_da_sub_base_sem_duplicar_token():
    with Session(_db()) as db:
        assert contas_por_base(db, "SB1") == {
            225: [{"canal": "Mercado Livre", "conta": "JSSICA"}],
            97: [{"canal": "Shopee", "conta": "mbi.store"}],
        }


def test_item_e_resumo():
    contas = [{"canal": "Mercado Livre", "conta": "X"}]
    assert a_coletar_item([], None) is None
    assert a_coletar_item(contas, None) == {"total": 0, "mercado_livre": 0, "shopee": 0}
    assert a_coletar_item([], {"mercado_livre": 1, "shopee": 2}) == {"total": 3, "mercado_livre": 1, "shopee": 2}
    assert chave_base(" jrp ") == "JRP"
    itens = [
        {"a_coletar": {"total": 4, "mercado_livre": 4, "shopee": 0}},
        {"a_coletar": {"total": 0, "mercado_livre": 0, "shopee": 0}},
        {"a_coletar": None},
        {"a_coletar": {"total": 1, "mercado_livre": 0, "shopee": 1}},
    ]
    assert resumo_a_coletar(itens) == {"total": 5, "mercado_livre": 4, "shopee": 1, "sellers": 2}
