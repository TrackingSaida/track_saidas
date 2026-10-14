import base64
import os
import re
import zlib
from datetime import date
from decimal import Decimal
from io import BytesIO

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")

from PIL import Image

from etiqueta_identidade_service import resolver_logo_owner
from fechamento_pdf_service import _fmt_periodo, _fmt_pix, _render_fechamento_pdf, _status_amigavel


class _OwnerSemLogo:
    logo_object_key = None
    nome_fantasia = "Wallace ME"
    username = "wallace"
    sub_base = "WALLACE"
    slogan = ""


def test_logo_do_owner_sem_cadastro_nao_usa_fallback():
    assert resolver_logo_owner(None) is None
    assert resolver_logo_owner(_OwnerSemLogo()) is None


def test_rotulos_do_fechamento():
    assert _status_amigavel("PAGO") == "Pago"
    assert _status_amigavel("FECHADO") == "Gerado"
    assert _fmt_pix("11914776465") == "11 91477-6465"
    assert _fmt_pix("email@pix") == "email@pix"
    assert _fmt_periodo(date(2026, 9, 16), date(2026, 9, 30)) == "16 a 30 de setembro de 2026"


def _linha(dia, flex, shopee, avulso=0, canc=0, valor=10):
    return {
        "data": f"2026-09-{dia:02d}",
        "flex": flex,
        "shopee": shopee,
        "avulso": avulso,
        "g_total": 0,
        "total_feitos": flex + shopee + avulso,
        "total_cancelado": canc,
        "valor_feitos": Decimal(valor),
    }


def _texto(pdf: bytes) -> str:
    partes = []
    for trecho in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        bruto = trecho.group(1)
        if bruto.endswith(b"~>"):
            bruto = bruto[:-2]
        try:
            dados = zlib.decompress(base64.a85decode(bruto))
        except Exception:
            try:
                dados = zlib.decompress(bruto)
            except Exception:
                continue
        texto = dados.decode("latin-1", errors="ignore")
        texto = re.sub(r"\\([0-7]{1,3})", lambda item: chr(int(item.group(1), 8)), texto)
        partes.append(texto.replace(r"\(", "(").replace(r"\)", ")"))
    return "\n".join(partes)


def _paginas(pdf: bytes) -> int:
    return len(re.findall(br"/Type /Page[^s]", pdf))


def _png_logo() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (80, 40), (255, 255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_pdf_de_entregador_traz_faixa_e_total():
    pdf = _render_fechamento_pdf(
        codigo="FEC-202609-LIZUAMORIM-000176",
        status_label="Pago",
        entregador="LIZU.AMORIM",
        pix=_fmt_pix("11914776465"),
        periodo="16 a 30 de setembro de 2026",
        gerado_em="14/10/2026 20:15",
        emitente="Wallace Oliveira de Souza Neves - ME",
        slogan="Mais que entregas, conexões.",
        logo=_png_logo(),
        feitos=3,
        cancelados=1,
        pacotes_g=0,
        ajustes=Decimal("116.30"),
        valor_final=Decimal("2911.30"),
        valor_feitos=Decimal("2795.00"),
        valor_cancelados=Decimal("5.00"),
        valor_coletas=Decimal("0"),
        qtd_dias_coleta=0,
        valor_g=Decimal("0"),
        adicao=Decimal("165"),
        motivo_adicao="Restante que faltou",
        subtracao=Decimal("48.70"),
        motivo_subtracao="Pacote perdido",
        linhas=[_linha(16, 28, 24, valor=288), _linha(29, 18, 32, avulso=1, canc=1, valor=274)],
        coletas=[],
        g_shopee=0,
        g_flex=0,
        g_avulso=0,
    )
    assert pdf.startswith(b"%PDF")
    texto = _texto(pdf)
    assert "Fechamento de entregas" in texto
    assert "LIZU.AMORIM" in texto
    assert "TOTAL A PAGAR" in texto
    assert "FEC-202609-LIZUAMORIM-000176" in texto
    assert "11 91477-6465" in texto
    assert "1 cancelado (R$ 5,00) não entra no pagamento." in texto
    assert b"/Subtype /Image" in pdf
    assert _paginas(pdf) == 1


def test_pdf_de_entregador_quebra_pagina_com_muitos_dias():
    linhas = [_linha(dia, 1, 1, valor=10) for dia in range(1, 32)]
    pdf = _render_fechamento_pdf(
        codigo="FEC-TESTE",
        status_label="Gerado",
        entregador="MOTO",
        pix="",
        periodo="1 a 31 de outubro de 2026",
        gerado_em="14/10/2026 20:15",
        emitente="Base Teste",
        slogan="",
        logo=None,
        feitos=62,
        cancelados=0,
        pacotes_g=0,
        ajustes=0,
        valor_final=Decimal("100"),
        valor_feitos=Decimal("100"),
        valor_cancelados=0,
        valor_coletas=0,
        qtd_dias_coleta=0,
        valor_g=0,
        adicao=0,
        motivo_adicao="",
        subtracao=0,
        motivo_subtracao="",
        linhas=linhas,
        coletas=[],
        g_shopee=0,
        g_flex=0,
        g_avulso=0,
    )
    assert _paginas(pdf) == 2
    assert b"/Subtype /Image" not in pdf
    texto = _texto(pdf)
    assert texto.count("Fechamento de entregas") == 2
    assert "01/09/2026" in texto
    assert "30/09/2026" in texto
