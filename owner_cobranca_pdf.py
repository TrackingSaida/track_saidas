"""Demonstrativo de utilização do Owner (PDF), lido só do snapshot do fechamento.

Não é nota, boleto nem documento fiscal.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from decimal import Decimal
from typing import Iterable, List, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from owner_cobranca_engine import CLASSIFICACAO_LABELS, MODELO_LABELS

COR_TEXTO = colors.HexColor("#0F172A")
COR_DESTAQUE = colors.HexColor("#0B1F3A")
COR_APOIO = colors.HexColor("#64748B")
COR_BORDA = colors.HexColor("#E2E8F0")

AJUSTE_LABELS = {"acrescimo": "Acréscimo", "desconto": "Desconto", "credito": "Crédito", "correcao": "Correção"}
STATUS_LABELS = {"fechado": "Fechado", "informado": "Informado", "reaberto": "Reaberto (versão substituída)"}


def _brl(v) -> str:
    n = Decimal(str(v or 0)).quantize(Decimal("0.01"))
    sinal = "-" if n < 0 else ""
    s = f"{abs(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sinal}R$ {s}"


def _preco(v) -> str:
    n = Decimal(str(v or 0)).normalize()
    casas = max(2, -n.as_tuple().exponent if n.as_tuple().exponent < 0 else 0)
    s = f"{n:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _int(v) -> str:
    return f"{int(v or 0):,}".replace(",", ".")


def _data(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def numero_fechamento(fech) -> str:
    return f"COB-{fech.periodo_inicio:%Y%m}-{int(fech.id):06d}-v{int(fech.versao)}"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s or "owner").strip("_") or "owner"


def nome_arquivo_demonstrativo(fech) -> str:
    return (
        f"demonstrativo_{_slug(fech.sub_base)}_{fech.periodo_inicio:%Y%m%d}_"
        f"{fech.periodo_fim:%Y%m%d}_v{fech.versao}.pdf"
    )


def nome_arquivo_csv(fech) -> str:
    return (
        f"cobranca_{_slug(fech.sub_base)}_{fech.periodo_inicio:%Y%m%d}_"
        f"{fech.periodo_fim:%Y%m%d}_v{fech.versao}.csv"
    )


def _estilos():
    base = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle("titulo", parent=base["Title"], fontName="Helvetica-Bold", fontSize=16,
                                 textColor=COR_DESTAQUE, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontName="Helvetica", fontSize=9, textColor=COR_APOIO),
        "rot": ParagraphStyle("rot", parent=base["Normal"], fontName="Helvetica", fontSize=8, textColor=COR_APOIO),
        "val": ParagraphStyle("val", parent=base["Normal"], fontName="Helvetica", fontSize=10, textColor=COR_TEXTO),
        "sec": ParagraphStyle("sec", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10,
                              textColor=COR_TEXTO, spaceBefore=10, spaceAfter=4),
        "total": ParagraphStyle("total", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=18,
                                textColor=COR_DESTAQUE, alignment=TA_RIGHT),
        "nota": ParagraphStyle("nota", parent=base["Normal"], fontName="Helvetica", fontSize=8,
                               textColor=COR_APOIO, leading=11),
    }


def gerar_demonstrativo_pdf(fech, *, composicao: Sequence, ajustes: Iterable) -> bytes:
    st = _estilos()
    owner = fech.owner_snapshot or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title="ROTEVO — Demonstrativo de utilização", author="ROTEVO",
    )
    largura = doc.width
    el: List = []

    el.append(Paragraph("ROTEVO — Demonstrativo de utilização", st["titulo"]))
    el.append(Paragraph("Documento informativo. Não é nota fiscal, boleto nem cobrança bancária.", st["sub"]))
    el.append(Spacer(1, 8 * mm))

    def bloco(rotulo, valor):
        return [Paragraph(rotulo, st["rot"]), Paragraph(valor or "—", st["val"])]

    nome = owner.get("razao_social") or owner.get("nome") or fech.sub_base
    ident = [
        bloco("Owner", nome) + bloco("Número", numero_fechamento(fech)),
        bloco("Documento", owner.get("documento")) + bloco("Versão", str(fech.versao)),
        bloco("Período", f"{_data(fech.periodo_inicio)} a {_data(fech.periodo_fim)}")
        + bloco("Emissão", datetime.now().strftime("%d/%m/%Y %H:%M")),
        bloco("Modelo comercial", MODELO_LABELS.get(fech.modelo, fech.modelo))
        + bloco("Fechado em", fech.fechado_em.strftime("%d/%m/%Y %H:%M") if fech.fechado_em else "—"),
    ]
    if owner.get("endereco"):
        ident.append(bloco("Endereço", owner.get("endereco")) + bloco("Situação", STATUS_LABELS.get(fech.status, fech.status)))
    t = Table(
        [[r[0], r[1], r[2], r[3]] for r in ident],
        colWidths=[largura * 0.16, largura * 0.44, largura * 0.14, largura * 0.26],
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    el.append(t)

    el.append(Paragraph("Composição", st["sec"]))
    linhas = [["Classificação", "Pacotes", "Valor"]]
    for c in composicao:
        if c.classificacao in ("duplicidade", "ajuste", "cancelado"):
            continue
        linhas.append([
            CLASSIFICACAO_LABELS.get(c.classificacao, c.classificacao),
            _int(c.quantidade),
            _brl(c.valor),
        ])
    preco = Decimal(str(fech.preco_unitario))
    idx_subtotal = len(linhas)
    linhas.append(["Subtotal bruto", _int(fech.qtd_bruto), _brl(preco * fech.qtd_bruto)])
    if fech.qtd_cancelados:
        linhas.append([
            "(−) Cancelados", f"−{_int(fech.qtd_cancelados)}", _brl(-preco * fech.qtd_cancelados),
        ])
    linhas.append(["Pacotes faturáveis", _int(fech.qtd_faturavel), _brl(fech.valor_pacotes)])
    tc = Table(linhas, colWidths=[largura * 0.56, largura * 0.18, largura * 0.26])
    n = len(linhas)
    tc.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), COR_APOIO),
        ("TEXTCOLOR", (0, 1), (-1, -1), COR_TEXTO),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, COR_BORDA),
        ("LINEABOVE", (0, idx_subtotal), (-1, idx_subtotal), 0.6, COR_BORDA),
        ("LINEABOVE", (0, n - 1), (-1, n - 1), 0.6, COR_BORDA),
        ("FONTNAME", (0, n - 1), (-1, n - 1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]))
    el.append(tc)
    el.append(Spacer(1, 2 * mm))
    el.append(Paragraph(
        f"Preço unitário do período: {_preco(fech.preco_unitario)} por pacote. "
        "Cancelados aparecem só como dedução do subtotal bruto.",
        st["nota"],
    ))

    lista_aj = list(ajustes)
    if lista_aj:
        el.append(Paragraph("Ajustes", st["sec"]))
        la = [["Tipo", "Motivo", "Valor"]]
        for a in lista_aj:
            la.append([AJUSTE_LABELS.get(a.tipo, a.tipo), Paragraph(a.motivo or "", st["nota"]), _brl(a.valor)])
        ta = Table(la, colWidths=[largura * 0.18, largura * 0.56, largura * 0.26])
        ta.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (-1, 0), COR_APOIO),
            ("ALIGN", (2, 0), (2, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, COR_BORDA),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
        ]))
        el.append(ta)

    el.append(Spacer(1, 8 * mm))
    tt = Table(
        [[Paragraph("Total do período", st["val"]), Paragraph(_brl(fech.valor_total), st["total"])]],
        colWidths=[largura * 0.5, largura * 0.5],
    )
    tt.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 1, COR_DESTAQUE),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]))
    el.append(tt)

    if fech.observacao:
        el.append(Paragraph("Observações", st["sec"]))
        el.append(Paragraph(fech.observacao, st["nota"]))

    el.append(Spacer(1, 10 * mm))
    el.append(Paragraph(
        f"Valores calculados a partir do fechamento {numero_fechamento(fech)} e não mudam com a operação posterior. "
        f"Conferência: {fech.hash[:16]}.",
        st["nota"],
    ))

    doc.build(el)
    return buf.getvalue()
