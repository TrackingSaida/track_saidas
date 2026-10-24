"""PDF oficial do Mercado Livre em POST /etiquetas/gerar, com fallback no gerador próprio."""
from __future__ import annotations

import io
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
os.environ.setdefault("SECRET_KEY", "teste-etiqueta-oficial")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from etiquetas_routes import EtiquetaGerarPayload, _pdf_oficial_ml, gerar_etiqueta
from ml_int_service import _pdf_de_conteudo_ml, obter_pdf_etiqueta_ml
from models import BasePreco, MLConexao, Saida


def _zip_com_pdf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("leia.txt", "nao e pdf")
        zf.writestr("etiqueta.pdf", b"%PDF-do-zip")
    return buf.getvalue()


def test_pdf_cru_e_zip():
    assert _pdf_de_conteudo_ml(b"%PDF-cru") == b"%PDF-cru"
    assert _pdf_de_conteudo_ml(_zip_com_pdf()) == b"%PDF-do-zip"
    assert _pdf_de_conteudo_ml(b"nao-e-pdf") is None
    assert _pdf_de_conteudo_ml(b"") is None


def _tabela(conn, model):
    cols = ", ".join(
        f"{c.name} INTEGER PRIMARY KEY" if c.primary_key else c.name for c in model.__table__.columns
    )
    conn.execute(text(f"CREATE TABLE {model.__tablename__} ({cols})"))


def _db_contas():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for model in (BasePreco, MLConexao, Saida):
            _tabela(conn, model)
        conn.execute(text(
            "INSERT INTO base (id_base, sub_base, base) VALUES "
            "(225, 'SB1', 'JRP'), (999, 'SB1', 'OUTRA')"
        ))
        conn.execute(text(
            "INSERT INTO ml_conexoes (id, sub_base, user_id_ml, id_base, criado_em) VALUES "
            "(1, 'SB1', 40, 999, '2026-10-02'), "
            "(2, 'SB1', 10, 225, '2026-10-01'), "
            "(3, 'SB1', 20, NULL, '2026-10-03'), "
            "(4, 'SB2', 30, 225, '2026-10-04')"
        ))
        conn.execute(text(
            "INSERT INTO saidas (id_saida, sub_base, base, ml_shipment_id) VALUES "
            "(1, 'SB1', 'JRP', 4825598)"
        ))
    return Session(engine)


def test_pdf_oficial_ignora_outro_tenant_e_prioriza_a_base(monkeypatch):
    db = _db_contas()
    saida = db.get(Saida, 1)
    chamadas = []

    def token(_db, user_id_ml, sub_base):
        chamadas.append(("token", user_id_ml, sub_base))
        return "tok"

    def pdf(_token, _sid, **kwargs):
        user_id = kwargs.get("user_id_ml")
        chamadas.append(("pdf", user_id, kwargs.get("sub_base")))
        if user_id == 10:
            raise ValueError("sem pdf")
        return b"%PDF-oficial"

    monkeypatch.setattr("ml_int_service.get_valid_access_token", token)
    monkeypatch.setattr("ml_int_service.fetch_shipment_label_pdf", pdf)

    assert obter_pdf_etiqueta_ml(db, saida) == b"%PDF-oficial"
    assert [c[1] for c in chamadas if c[0] == "pdf"] == [10, 40]
    assert all(c[2] == "SB1" for c in chamadas)


def test_sem_pdf_devolve_none(monkeypatch):
    db = _db_contas()
    saida = db.get(Saida, 1)

    def pdf(*_a, **_k):
        raise ValueError("corpo invalido")

    monkeypatch.setattr("ml_int_service.get_valid_access_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr("ml_int_service.fetch_shipment_label_pdf", pdf)
    assert obter_pdf_etiqueta_ml(db, saida) is None


def _user():
    return SimpleNamespace(sub_base="SB1", role=1, motoboy_id=None)


def _saida(**kwargs):
    dados = dict(
        id_saida=1,
        sub_base="SB1",
        base="JRP",
        codigo="45800000001",
        ml_shipment_id=4825598,
        qr_payload_raw=None,
        servico="Mercado Livre",
        motoboy_id=None,
    )
    dados.update(kwargs)
    return SimpleNamespace(**dados)


def test_gerar_devolve_pdf_oficial_e_grava_qr(monkeypatch):
    saida = _saida()
    db = MagicMock()
    db.get.return_value = saida
    monkeypatch.setattr("etiquetas_routes._pdf_oficial_ml", lambda *_a, **_k: b"%PDF-oficial")
    monkeypatch.setattr(
        "ml_int_service.obter_qr_etiqueta_ml",
        lambda *_a, **_k: '{"id":"4825598","sender_id":10}',
    )

    resp = gerar_etiqueta(
        EtiquetaGerarPayload(codigo="45800000001", id_saida=1),
        db,
        _user(),
    )
    assert resp.body == b"%PDF-oficial"
    assert resp.media_type == "application/pdf"
    assert saida.qr_payload_raw == '{"id":"4825598","sender_id":10}'
    db.commit.assert_called()


def test_falha_do_ml_cai_no_gerador_proprio(monkeypatch):
    saida = _saida()
    db = MagicMock()
    db.get.return_value = saida
    db.scalar.return_value = None
    monkeypatch.setattr("etiquetas_routes._pdf_oficial_ml", lambda *_a, **_k: None)

    def proprio(**kwargs):
        assert kwargs["formato"] == "pdf"
        return b"%PDF-proprio"

    monkeypatch.setattr("etiqueta_pdf_service.gerar_etiqueta", proprio)
    resp = gerar_etiqueta(
        EtiquetaGerarPayload(codigo="45800000001", id_saida=1),
        db,
        _user(),
    )
    assert resp.body == b"%PDF-proprio"


def test_png_nao_pede_pdf_oficial(monkeypatch):
    saida = _saida()
    assert _pdf_oficial_ml(MagicMock(), saida, "png") is None

    db = MagicMock()
    db.get.return_value = saida
    db.scalar.return_value = None
    pedido = []
    monkeypatch.setattr(
        "ml_int_service.obter_pdf_etiqueta_ml",
        lambda *_a, **_k: pedido.append(1) or b"%PDF",
    )
    monkeypatch.setattr("etiqueta_pdf_service.gerar_etiqueta", lambda **_k: b"png")
    resp = gerar_etiqueta(
        EtiquetaGerarPayload(codigo="45800000001", id_saida=1, formato="png"),
        db,
        _user(),
    )
    assert pedido == []
    assert resp.media_type == "image/png"
