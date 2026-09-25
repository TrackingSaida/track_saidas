"""Testes da importação em massa de Bases (PRD-002)."""
from __future__ import annotations

import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from base_import_service import (
    ACAO_ATUALIZAR,
    ACAO_CRIAR,
    ACAO_ERRO,
    classificar_linhas,
    confirmar_importacao,
    gerar_modelo_xlsx,
    ler_linhas_xlsx,
    normalize_base_nome,
    parse_moeda,
    preview_importacao,
)


def _xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_normalize_base_nome():
    assert normalize_base_nome("  a.l.  ") == "A.L."
    assert normalize_base_nome("Nova   Base") == "NOVA BASE"


def test_parse_moeda_formatos_br():
    assert parse_moeda(8) == 8.0
    assert parse_moeda(8.5) == 8.5
    assert parse_moeda("8,50") == 8.5
    assert parse_moeda("R$ 8,00") == 8.0
    assert parse_moeda("1.234,56") == 1234.56
    assert parse_moeda(" 9 ") == 9.0


def test_parse_moeda_invalido():
    with pytest.raises(ValueError):
        parse_moeda("")
    with pytest.raises(ValueError):
        parse_moeda(-1)
    with pytest.raises(ValueError):
        parse_moeda("abc")


def test_gerar_modelo_tem_cabecalhos():
    content = gerar_modelo_xlsx()
    linhas = ler_linhas_xlsx(content)
    # modelo tem 1 linha de exemplo
    assert len(linhas) == 1
    assert normalize_base_nome(str(linhas[0]["base"])) == "EXEMPLO BASE"


def test_ler_xlsx_exige_colunas():
    content = _xlsx_bytes([["nome", "preco"], ["A", 1]])
    with pytest.raises(HTTPException) as exc:
        ler_linhas_xlsx(content)
    assert exc.value.status_code == 400
    assert "colunas obrigatórias" in str(exc.value.detail).lower()


def test_ler_xlsx_ignora_linha_em_branco():
    content = _xlsx_bytes(
        [
            ["base", "flex", "shopee", "avulso"],
            ["A.L.", 8, 5, 8],
            [None, None, None, None],
            ["", "", "", ""],
            ["B.B.", 9, 6, 9],
        ]
    )
    linhas = ler_linhas_xlsx(content)
    assert len(linhas) == 2
    assert linhas[0]["linha"] == 2
    assert linhas[1]["linha"] == 5


def test_classificar_criar_atualizar_erro_e_duplicata():
    existente = SimpleNamespace(id_base=41, base="A.L.", ml=7.0, shopee=5.0, avulso=7.0)
    existentes = {"A.L.": existente}
    brutas = [
        {"linha": 2, "base": "a.l.", "flex": "8,00", "shopee": 5, "avulso": 8},
        {"linha": 3, "base": "Nova", "flex": 9, "shopee": 6, "avulso": 9},
        {"linha": 4, "base": "", "flex": 1, "shopee": 1, "avulso": 1},
        {"linha": 5, "base": "Nova", "flex": 1, "shopee": 1, "avulso": 1},
        {"linha": 6, "base": "Ruim", "flex": "x", "shopee": 1, "avulso": 1},
    ]
    preview = classificar_linhas(brutas, existentes)
    assert preview.resumo == {"criar": 1, "atualizar": 1, "erro": 3, "total_linhas": 5}

    by_line = {l.linha: l for l in preview.linhas}
    assert by_line[2].acao == ACAO_ATUALIZAR
    assert by_line[2].id_base == 41
    assert by_line[2].atual == {"flex": 7.0, "shopee": 5.0, "avulso": 7.0}
    assert by_line[2].flex == 8.0

    assert by_line[3].acao == ACAO_CRIAR
    assert by_line[3].base == "NOVA"

    assert by_line[4].acao == ACAO_ERRO
    assert "obrigatório" in (by_line[4].motivo or "").lower()

    assert by_line[5].acao == ACAO_ERRO
    assert "repetido" in (by_line[5].motivo or "").lower()

    assert by_line[6].acao == ACAO_ERRO


def test_preview_importacao_com_db_mock():
    content = _xlsx_bytes(
        [
            ["base", "flex", "shopee", "avulso"],
            ["Nova Base", 8, 5, 8],
        ]
    )
    db = MagicMock()
    result_proxy = MagicMock()
    result_proxy.all.return_value = []
    db.scalars.return_value = result_proxy

    out = preview_importacao(db, "TENANT_A", content)
    assert out["ok"] is True
    assert out["resumo"]["criar"] == 1
    assert out["linhas"][0]["acao"] == ACAO_CRIAR
    assert out["linhas"][0]["base"] == "NOVA BASE"


def test_confirmar_cria_e_atualiza():
    existente = SimpleNamespace(
        id_base=41,
        base="A.L.",
        sub_base="TENANT_A",
        ml=7.0,
        shopee=5.0,
        avulso=7.0,
        ativo=False,
    )
    db = MagicMock()
    result_proxy = MagicMock()
    result_proxy.all.return_value = [existente]
    db.scalars.return_value = result_proxy

    # flush atribui id na criação
    created = []

    def fake_add(obj):
        if getattr(obj, "id_base", None) is None and getattr(obj, "base", None) != "A.L.":
            obj.id_base = 99
            created.append(obj)

    def fake_flush():
        pass

    db.add.side_effect = fake_add
    db.flush.side_effect = fake_flush

    user = SimpleNamespace(username="admin")
    out = confirmar_importacao(
        db,
        "TENANT_A",
        user,
        [
            {"linha": 2, "base": "A.L.", "flex": 8, "shopee": 5, "avulso": 8},
            {"linha": 3, "base": "Nova", "flex": 9, "shopee": 6, "avulso": 9},
        ],
    )

    assert out["criadas"] == 1
    assert out["atualizadas"] == 1
    assert out["erros"] == 0
    assert existente.ml == 8.0
    assert existente.avulso == 8.0
    assert existente.ativo is False  # update não mexe em ativo
    assert len(created) == 1
    assert created[0].base == "NOVA"
    assert created[0].ativo is True
    db.commit.assert_called_once()


def test_confirmar_lista_vazia():
    db = MagicMock()
    user = SimpleNamespace(username="admin")
    with pytest.raises(HTTPException) as exc:
        confirmar_importacao(db, "TENANT_A", user, [])
    assert exc.value.status_code == 400
