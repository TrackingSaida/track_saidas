"""Demonstrativo de utilização do Owner (PDF), lido só do snapshot do fechamento.

Não é nota, boleto nem documento fiscal.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, List, Sequence, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from owner_cobranca_engine import (
    CLASSIFICACAO_LABELS,
    EXPLICACAO_MODELO,
    MODELO_LABELS,
    ORIGEM_LABELS,
    SERVICO_LABELS,
)

COR_TEXTO = colors.HexColor("#0F172A")
COR_DESTAQUE = colors.HexColor("#0B1F3A")
COR_APOIO = colors.HexColor("#64748B")
COR_BORDA = colors.HexColor("#E2E8F0")
COR_FUNDO = colors.HexColor("#F1F5F9")

CLASSES_AJUSTE = ("ajuste", "ajuste_periodo", "isencao")
AJUSTE_LABELS = {
    "acrescimo": "Acréscimo",
    "desconto": "Desconto",
    "credito": "Crédito",
    "correcao": "Correção",
    "isencao": "Isenção",
}
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
        "texto": ParagraphStyle("texto", parent=base["Normal"], fontName="Helvetica", fontSize=9,
                                textColor=COR_TEXTO, leading=13),
    }


def _documento(v) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return str(v or "")


def _ajuste_label(a) -> str:
    label = AJUSTE_LABELS.get(a.tipo, a.tipo)
    pct = getattr(a, "percentual", None)
    if pct is not None:
        label = f"{label} de {format(Decimal(str(pct)).normalize(), 'f').replace('.', ',')}%"
    return label


def _tabela_simples(linhas, larguras, *, alinhar_direita=(), negrito_ultima=False):
    t = Table(linhas, colWidths=larguras, repeatRows=1)
    estilo = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), COR_APOIO),
        ("TEXTCOLOR", (0, 1), (-1, -1), COR_TEXTO),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, COR_BORDA),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]
    for col in alinhar_direita:
        estilo.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    if negrito_ultima:
        estilo += [
            ("LINEABOVE", (0, -1), (-1, -1), 0.6, COR_BORDA),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ]
    t.setStyle(TableStyle(estilo))
    return t


def gerar_demonstrativo_pdf(
    fech,
    *,
    composicao: Sequence,
    ajustes: Iterable,
    situacao: Sequence[dict] = (),
    por_dia: Sequence[Tuple[date, int]] = (),
    itens: Sequence = (),
    limite_anexo: int = 3000,
) -> bytes:
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
    el.append(Spacer(1, 6 * mm))

    def bloco(rotulo, valor, vazio=""):
        return [Paragraph(rotulo, st["rot"]), Paragraph(valor or vazio, st["val"])]

    nome = owner.get("razao_social") or owner.get("nome") or fech.sub_base
    ident = [
        bloco("Owner", nome) + bloco("Número", numero_fechamento(fech)),
        bloco("CNPJ/CPF", _documento(owner.get("documento"))) + bloco("Versão", str(fech.versao)),
        bloco("Endereço", owner.get("endereco"))
        + bloco("Período", f"{_data(fech.periodo_inicio)} a {_data(fech.periodo_fim)}"),
        bloco("E-mail", owner.get("email")) + bloco("Emissão", datetime.now().strftime("%d/%m/%Y %H:%M")),
        bloco("Telefone", owner.get("telefone") or owner.get("contato"))
        + bloco("Situação", STATUS_LABELS.get(fech.status, fech.status)),
    ]
    if owner.get("contato_financeiro"):
        ident.append(
            bloco("Contato financeiro", owner.get("contato_financeiro"))
            + bloco("Fechado em", fech.fechado_em.strftime("%d/%m/%Y %H:%M") if fech.fechado_em else "")
        )
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

    el.append(Spacer(1, 4 * mm))
    resumo = Table(
        [[
            Paragraph(
                f"{_int(fech.qtd_faturavel)} pacotes × {_preco(fech.preco_unitario)}"
                + (" + ajustes" if Decimal(str(fech.valor_ajustes or 0)) != 0 else ""),
                st["val"],
            ),
            Paragraph(_brl(fech.valor_total), st["total"]),
        ]],
        colWidths=[largura * 0.6, largura * 0.4],
    )
    resumo.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COR_FUNDO),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    el.append(resumo)

    el.append(Paragraph("Como calculamos", st["sec"]))
    el.append(Paragraph(
        f"Modelo comercial: <b>{MODELO_LABELS.get(fech.modelo, fech.modelo)}</b>. "
        f"{EXPLICACAO_MODELO.get(fech.modelo, '')} "
        "Pacotes cancelados e leituras repetidas não são cobrados.",
        st["texto"],
    ))

    el.append(Paragraph("Composição", st["sec"]))
    linhas = [["Classificação", "Pacotes", "Valor"]]
    for c in composicao:
        if c.classificacao in ("duplicidade", "cancelado") or c.classificacao in CLASSES_AJUSTE:
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

    lista_sit = list(situacao)
    if lista_sit:
        el.append(Paragraph("O que aconteceu com os pacotes cobrados", st["sec"]))
        ls = [["Situação hoje", "Pacotes"]]
        for s in lista_sit:
            ls.append([s.get("label"), _int(s.get("quantidade"))])
        ls.append(["Total cobrado", _int(fech.qtd_faturavel)])
        el.append(_tabela_simples(ls, [largura * 0.74, largura * 0.26], alinhar_direita=(1,), negrito_ultima=True))
        el.append(Spacer(1, 2 * mm))
        el.append(Paragraph(
            "O pacote é cobrado quando entra na operação (coleta ou recebimento), mesmo que ainda "
            "não tenha saído para entrega. A situação mostra onde cada pacote está hoje.",
            st["nota"],
        ))

    lista_aj = list(ajustes)
    if lista_aj:
        el.append(Paragraph("Ajustes", st["sec"]))
        la = [["Tipo", "Motivo", "Valor"]]
        for a in lista_aj:
            la.append([_ajuste_label(a), Paragraph(a.motivo or "", st["nota"]), _brl(a.valor)])
        el.append(_tabela_simples(la, [largura * 0.22, largura * 0.52, largura * 0.26], alinhar_direita=(2,)))

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

    dias = list(por_dia)
    if dias:
        metade = (len(dias) + 1) // 2
        ld = [["Dia", "Pacotes", "", "Dia", "Pacotes"]]
        for i in range(metade):
            esq = dias[i]
            dir_ = dias[i + metade] if i + metade < len(dias) else None
            ld.append([
                _data(esq[0]), _int(esq[1]), "",
                _data(dir_[0]) if dir_ else "", _int(dir_[1]) if dir_ else "",
            ])
        el.append(KeepTogether([
            Paragraph("Pacotes cobrados por dia", st["sec"]),
            _tabela_simples(
                ld,
                [largura * 0.25, largura * 0.2, largura * 0.1, largura * 0.25, largura * 0.2],
                alinhar_direita=(1, 4),
            ),
        ]))

    el.append(Spacer(1, 8 * mm))
    el.append(Paragraph(
        f"Valores calculados a partir do fechamento {numero_fechamento(fech)} e não mudam com a operação posterior. "
        f"Conferência: {fech.hash[:16]}.",
        st["nota"],
    ))

    lista_itens = list(itens)
    if lista_itens:
        el.append(PageBreak())
        el.append(Paragraph("Anexo — pacotes cobrados", st["sec"]))
        excedeu = len(lista_itens) > limite_anexo
        li = [["Data", "Código", "Serviço", "Origem", "Qtd."]]
        for it in lista_itens[:limite_anexo]:
            li.append([
                _data(it.data_competencia),
                it.codigo or "Coleta manual",
                SERVICO_LABELS.get(it.servico or "", it.servico or ""),
                ORIGEM_LABELS.get(it.origem, it.origem or ""),
                _int(it.quantidade),
            ])
        tabela = _tabela_simples(
            li,
            [largura * 0.16, largura * 0.36, largura * 0.18, largura * 0.18, largura * 0.12],
            alinhar_direita=(4,),
        )
        tabela.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8)]))
        el.append(tabela)
        if excedeu:
            el.append(Spacer(1, 2 * mm))
            el.append(Paragraph(
                f"Lista limitada aos primeiros {_int(limite_anexo)} itens. A lista completa está no arquivo CSV.",
                st["nota"],
            ))

    doc.build(el)
    return buf.getvalue()
