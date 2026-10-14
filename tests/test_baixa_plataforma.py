import json
from datetime import datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from baixa_plataforma_pure import (
    ACAO_CANCELAR,
    ACAO_ENTREGAR,
    ACAO_SINALIZAR_CANCELADO,
    FINAL_CANCELADO,
    FINAL_ENTREGUE,
    acao_baixa,
    ml_status_final,
    rotulo_evento_plataforma,
    shopee_status_final,
)
from models import Saida, SaidaHistorico
from qr_payload_utils import extrair_qr_ml_de_zpl

QR_ML = '{"id":"48246684477","t":"lm","sender_id":123456,"hash_code":"abcXYZ=","security_digit":"0"}'


def test_status_final_ml():
    assert ml_status_final({"status": "delivered"}) == FINAL_ENTREGUE
    assert ml_status_final({"status": "cancelled"}) == FINAL_CANCELADO
    assert ml_status_final({"status": "shipped"}) is None
    assert ml_status_final({}) is None


def test_status_final_shopee():
    assert shopee_status_final({"order_status": "COMPLETED"}) == FINAL_ENTREGUE
    assert shopee_status_final({"order_status": "TO_CONFIRM_RECEIVE"}) == FINAL_ENTREGUE
    assert shopee_status_final({"order_status": "CANCELLED"}) == FINAL_CANCELADO
    assert shopee_status_final({"order_status": "IN_CANCEL"}) is None
    assert shopee_status_final({"order_status": "SHIPPED"}) is None


def test_acao_baixa():
    assert acao_baixa("aguardando_coleta", FINAL_ENTREGUE) == ACAO_CANCELAR
    assert acao_baixa("aguardando_coleta", FINAL_CANCELADO) == ACAO_CANCELAR
    assert acao_baixa("coletado", FINAL_ENTREGUE) == ACAO_ENTREGAR
    assert acao_baixa("em_rota", FINAL_ENTREGUE) == ACAO_ENTREGAR
    assert acao_baixa("ausente", FINAL_ENTREGUE) == ACAO_ENTREGAR
    assert acao_baixa("coletado", FINAL_CANCELADO) == ACAO_SINALIZAR_CANCELADO
    assert acao_baixa("coletado", FINAL_CANCELADO, ja_sinalizado=True) is None
    assert acao_baixa("entregue", FINAL_CANCELADO) is None
    assert acao_baixa("Cancelado", FINAL_ENTREGUE) is None
    assert acao_baixa("coletado", None) is None


def test_rotulos_por_plataforma():
    assert rotulo_evento_plataforma("entregue", "Mercado Livre") == "Entrega confirmada pelo Mercado Livre"
    assert rotulo_evento_plataforma("cancelado", "Shopee") == "Cancelado pela Shopee (não coletado)"
    assert rotulo_evento_plataforma("cancelado_plataforma", "Shopee") == "Cancelado na Shopee: verificar devolução"
    assert rotulo_evento_plataforma("entregue", None) is None


def _db():
    engine = create_engine("sqlite://")
    cols_saida = ", ".join(
        f"{c.name} INTEGER PRIMARY KEY" if c.primary_key else c.name for c in Saida.__table__.columns
    )
    cols_hist = ", ".join(
        f"{c.name} INTEGER PRIMARY KEY" if c.primary_key else c.name for c in SaidaHistorico.__table__.columns
    )
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE saidas ({cols_saida})"))
        conn.execute(text(f"CREATE TABLE saida_historico ({cols_hist})"))
        conn.execute(text(
            "INSERT INTO saidas (id_saida, timestamp, data, status, is_grande, avulso_criado_excepcional) VALUES "
            "(1, '2026-10-13 10:00:00', '2026-10-13', 'em_rota', 0, 0), "
            "(2, '2026-10-13 10:00:00', '2026-10-13', 'aguardando_coleta', 0, 0), "
            "(3, '2026-10-13 10:00:00', '2026-10-13', 'coletado', 0, 0), "
            "(4, '2026-10-13 10:00:00', '2026-10-13', 'entregue', 0, 0)"
        ))
    return engine


def _aplicar(db, id_saida, final, plataforma="Mercado Livre"):
    from baixa_plataforma_service import aplicar_baixa

    with db.begin_nested():
        acao = aplicar_baixa(db, db.get(Saida, id_saida), final, plataforma)
    db.commit()
    return acao


def test_aplicar_baixa_entregue_cancelado_e_sinalizado():
    with Session(_db()) as db:
        assert _aplicar(db, 1, FINAL_ENTREGUE) == ACAO_ENTREGAR
        assert _aplicar(db, 2, FINAL_ENTREGUE, "Shopee") == ACAO_CANCELAR
        assert _aplicar(db, 3, FINAL_CANCELADO) == ACAO_SINALIZAR_CANCELADO
        assert _aplicar(db, 3, FINAL_CANCELADO) is None
        assert _aplicar(db, 4, FINAL_CANCELADO) is None

        status = {s.id_saida: s.status for s in db.scalars(select(Saida)).all()}
        assert status == {1: "entregue", 2: "cancelado", 3: "coletado", 4: "entregue"}
        assert db.get(Saida, 1).data_hora_entrega is not None
        assert isinstance(db.get(Saida, 3).plataforma_cancelado_em, datetime)

        hist = db.scalars(select(SaidaHistorico).order_by(SaidaHistorico.id)).all()
        assert [(h.id_saida, h.evento, h.status_anterior, h.status_novo) for h in hist] == [
            (1, "entregue", "em_rota", "entregue"),
            (2, "cancelado", "aguardando_coleta", "cancelado"),
            (3, "cancelado_plataforma", "coletado", "coletado"),
        ]
        assert json.loads(hist[1].payload) == {"plataforma": "Shopee"}


def test_extrair_qr_ml_de_zpl():
    zpl = (
        "^XA^FO50,50^A0N,30,30^FDMERCADO ENVIOS^FS"
        f"^FO30,200^BQN,2,5^FDLA,{QR_ML}^FS^XZ"
    )
    assert extrair_qr_ml_de_zpl(zpl, 48246684477) == QR_ML
    assert extrair_qr_ml_de_zpl(zpl, 11111111111) is None
    assert extrair_qr_ml_de_zpl("^XA^FD48246684477^FS^XZ", 48246684477) is None
    assert extrair_qr_ml_de_zpl(None, 1) is None


def test_extrair_qr_ml_de_zpl_com_hex():
    codificado = QR_ML.replace('"', "_22").replace("{", "_7B").replace("}", "_7D")
    zpl = f"^XA^FO30,200^BQN,2,5^FH^FDLA,{codificado}^FS^XZ"
    assert extrair_qr_ml_de_zpl(zpl, "48246684477") == QR_ML
