"""Testes da exportação de Registros (CSV/XLSX) — lógica pura."""
from __future__ import annotations

from datetime import datetime

import pytest

from saidas_exportar_service import (
    EXPORT_COLUMN_KEYS,
    MAX_EXPORTAR_LIMIT,
    MSG_FORMATO,
    MSG_SEM_COLUNAS,
    MSG_SEM_REGISTROS,
    MSG_TETO,
    ExportarSaidasError,
    content_disposition,
    formatar_status_export,
    gerar_csv,
    gerar_xlsx,
    montar_linhas,
    nome_arquivo_export,
    resolver_colunas,
    resolver_formato,
    validar_total_exportacao,
)
from saidas_listar_service import MAX_LISTAR_LIMIT, clamp_listar_limit


def test_clamp_listar_limit_cap_exportacao():
    assert clamp_listar_limit(9999) == MAX_LISTAR_LIMIT
    assert clamp_listar_limit(9999, cap=MAX_EXPORTAR_LIMIT) == 9999
    assert clamp_listar_limit(20000, cap=MAX_EXPORTAR_LIMIT) == MAX_EXPORTAR_LIMIT


def test_resolver_formato_e_colunas():
    assert resolver_formato("CSV") == "csv"
    assert resolver_formato("xlsx") == "xlsx"
    assert resolver_colunas(None) == list(EXPORT_COLUMN_KEYS)
    assert resolver_colunas(["codigo", "status", "codigo", "invalida"]) == ["codigo", "status"]
    with pytest.raises(ExportarSaidasError, match=MSG_FORMATO):
        resolver_formato("pdf")
    with pytest.raises(ExportarSaidasError, match=MSG_SEM_COLUNAS):
        resolver_colunas([])
    with pytest.raises(ExportarSaidasError, match=MSG_SEM_COLUNAS):
        resolver_colunas(["foo"])


def test_validar_total_exportacao():
    validar_total_exportacao(1)
    validar_total_exportacao(MAX_EXPORTAR_LIMIT)
    with pytest.raises(ExportarSaidasError, match=MSG_SEM_REGISTROS):
        validar_total_exportacao(0)
    with pytest.raises(ExportarSaidasError, match=MSG_TETO):
        validar_total_exportacao(MAX_EXPORTAR_LIMIT + 1)


def test_formatar_status_e_linhas():
    assert formatar_status_export("em_rota") == "EM ROTA"
    assert formatar_status_export("na_base") == "Na Base"
    assert formatar_status_export("saiu") == "SAIU PARA ENTREGA"
    assert formatar_status_export("encerrado_sistema") == "Encerrado"
    item = {
        "codigo": "BR123",
        "servico": "Shopee",
        "status": "em_rota",
        "acao": "Iniciou rota",
        "entregador": "Joao",
        "data_hora_acao": datetime(2026, 8, 27, 9, 15),
        "timestamp": datetime(2026, 8, 20, 8, 0),
        "executado_por": "ops",
        "base": "DRK",
        "is_grande": True,
    }
    linha = montar_linhas([item], ["codigo", "status", "is_grande", "data_hora_acao"])[0]
    assert linha == ["BR123", "EM ROTA", "Sim", "27/08/2026 09:15"]


def test_gerar_csv_separador_ponto_e_virgula_e_bom():
    items = [
        {
            "codigo": "BR001",
            "servico": "Shopee",
            "status": "entregue",
            "acao": "Finalizou entrega",
            "entregador": "Ana",
            "data_hora_acao": datetime(2026, 8, 27, 10, 0),
            "timestamp": datetime(2026, 8, 27, 8, 0),
            "executado_por": "ops",
            "base": "DRK",
            "is_grande": False,
        }
    ]
    raw = gerar_csv(["codigo", "status", "base"], items)
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "Código;Status;Base" in text
    assert "BR001;ENTREGUE;DRK" in text
    assert "," not in text.splitlines()[0]


def test_gerar_xlsx_e_nome_arquivo():
    items = [{"codigo": "41234567890", "status": "saiu", "is_grande": False}]
    payload = gerar_xlsx(["codigo", "status"], items)
    assert payload[:2] == b"PK"
    nome = nome_arquivo_export("csv", agora=datetime(2026, 8, 27, 9, 37))
    assert nome == "registros_2026-08-27_0937.csv"
    assert 'filename="registros_2026-08-27_0937.csv"' in content_disposition(nome)
