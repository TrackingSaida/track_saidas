"""
Gera PDF de fechamento de motoboy/entregador (reportlab) e grava no B2.

O layout espelha o relatório web (A4 paisagem): resumo, financeiro,
detalhamento diário, ajustes e pacotes grandes.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import EntregadorFechamento, Saida
from saida_operacional_utils import filtrar_saidas_por_periodo_operacional
from upload_storage_utils import B2_BUCKET_NAME, b2_configured, get_s3_client_optional

logger = logging.getLogger(__name__)

COR_HEADER = colors.Color(74 / 255, 46 / 255, 127 / 255)
STATUS_LABELS = {
    "PENDENTE": "PENDENTE",
    "GERADO": "GERADO",
    "REAJUSTADO": "REAJUSTADO",
    "PAGO": "PAGO",
    "FECHADO": "GERADO",
}


def _fmt_brl(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    s = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _fmt_signed(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    if n == 0:
        return _fmt_brl(0)
    sign = "+" if n > 0 else "-"
    return f"{sign}{_fmt_brl(abs(n))}"


def _fmt_desconto(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    return f"-{_fmt_brl(n)}" if n > 0 else _fmt_brl(0)


def _fmt_date(d) -> str:
    if not d:
        return "—"
    try:
        if hasattr(d, "strftime"):
            return d.strftime("%d/%m/%Y")
        parts = str(d).split("-")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
        return str(d)
    except Exception:
        return str(d)


def _normalize_status(status: Optional[str]) -> str:
    key = (status or "PENDENTE").upper()
    return STATUS_LABELS.get(key, key)


def build_fechamento_code(fech: EntregadorFechamento) -> str:
    periodo = fech.periodo_fim or fech.periodo_inicio
    key = periodo.strftime("%Y%m") if periodo else "000000"
    raw = (fech.username_entregador or "MOTOBOY").upper()
    raw = re.sub(r"[^A-Z0-9]+", "", raw)[:12] or "MOTOBOY"
    return f"FEC-{key}-{raw}-{int(fech.id_fechamento):06d}"


def _collect_itens_diarios(db: Session, fech: EntregadorFechamento) -> List[Dict[str, Any]]:
    """Monta itens diários no mesmo espírito do GET /entregadores/resumo."""
    from entregador_routes import (
        STATUS_VALOR_BASE_VALIDOS,
        _normalizar_servico,
        _resolve_executor_scope_ids,
        resolver_precos_entregador,
        resolver_precos_motoboy,
    )

    sub_base = fech.sub_base
    data_inicio = fech.periodo_inicio
    data_fim = fech.periodo_fim
    entregador_id = getattr(fech, "id_entregador", None)
    motoboy_id = getattr(fech, "id_motoboy", None)

    stmt = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.codigo.isnot(None),
        func.lower(Saida.status).in_(STATUS_VALOR_BASE_VALIDOS),
        or_(
            Saida.entregador_id.isnot(None),
            Saida.entregador.isnot(None),
            Saida.motoboy_id.isnot(None),
        ),
        Saida.timestamp >= datetime.combine(data_inicio, datetime.min.time()),
        Saida.timestamp < datetime.combine(data_fim + timedelta(days=1), datetime.min.time()),
    )

    entregador_ids, motoboy_ids = _resolve_executor_scope_ids(
        db=db,
        sub_base_user=sub_base,
        entregador_id=entregador_id,
        motoboy_id=motoboy_id,
    )
    conds = []
    if entregador_ids:
        conds.append(Saida.entregador_id.in_(sorted(entregador_ids)))
    if motoboy_ids:
        conds.append(Saida.motoboy_id.in_(sorted(motoboy_ids)))
    if not conds:
        return []
    stmt = stmt.where(or_(*conds))

    rows_raw = db.scalars(stmt).all()
    rows, op_ctx_map = filtrar_saidas_por_periodo_operacional(db, rows_raw, data_inicio, data_fim)

    agrupado: Dict[str, Dict[str, Any]] = {}
    for saida in rows:
        ctx = op_ctx_map.get(saida.id_saida)
        op_ts = (ctx.operacional_ts if ctx and ctx.operacional_ts else None) or saida.timestamp
        dia = op_ts.date().isoformat()
        if dia not in agrupado:
            agrupado[dia] = {
                "data": dia,
                "qtde_shopee": 0,
                "qtde_flex": 0,
                "qtde_avulso": 0,
                "cancel_shopee": 0,
                "cancel_flex": 0,
                "cancel_avulso": 0,
                "total_feitos": 0,
                "total_cancelado": 0,
                "g_total": 0,
                "g_shopee": 0,
                "g_flex": 0,
                "g_avulso": 0,
            }
        bucket = agrupado[dia]
        status_norm = (saida.status or "").strip().lower()
        is_cancelado = "cancel" in status_norm
        tipo = _normalizar_servico(saida.servico)
        if is_cancelado:
            bucket["total_cancelado"] += 1
            bucket[f"cancel_{tipo}"] += 1
        else:
            bucket["total_feitos"] += 1
            bucket[f"qtde_{tipo}"] += 1
        if getattr(saida, "is_grande", False):
            bucket["g_total"] += 1
            if tipo == "shopee":
                bucket["g_shopee"] += 1
            elif tipo == "flex":
                bucket["g_flex"] += 1
            else:
                bucket["g_avulso"] += 1

    if motoboy_id is not None:
        precos = resolver_precos_motoboy(db, sub_base, motoboy_id=motoboy_id)
    elif entregador_id is not None and int(entregador_id) > 0:
        precos = resolver_precos_entregador(db, entregador_id, sub_base)
    else:
        precos = resolver_precos_motoboy(db, sub_base)

    out: List[Dict[str, Any]] = []
    for dia in sorted(agrupado.keys()):
        item = agrupado[dia]
        valor_shopee = Decimal(item["qtde_shopee"]) * precos["shopee_valor"]
        valor_flex = Decimal(item["qtde_flex"]) * precos["ml_valor"]
        valor_avulso = Decimal(item["qtde_avulso"]) * precos["avulso_valor"]
        valor_feitos = (valor_shopee + valor_flex + valor_avulso).quantize(Decimal("0.01"))
        valor_cancelados = (
            Decimal(item["cancel_shopee"]) * precos["shopee_valor"]
            + Decimal(item["cancel_flex"]) * precos["ml_valor"]
            + Decimal(item["cancel_avulso"]) * precos["avulso_valor"]
        ).quantize(Decimal("0.01"))
        total_dia = (valor_feitos - valor_cancelados).quantize(Decimal("0.01"))
        out.append(
            {
                "data": dia,
                "flex": item["qtde_flex"],
                "shopee": item["qtde_shopee"],
                "avulso": item["qtde_avulso"],
                "g_total": item["g_total"],
                "g_shopee": item["g_shopee"],
                "g_flex": item["g_flex"],
                "g_avulso": item["g_avulso"],
                "total_feitos": item["total_feitos"],
                "total_cancelado": item["total_cancelado"],
                "valor_feitos": valor_feitos,
                "valor_cancelados": valor_cancelados,
                "valor_total": total_dia,
            }
        )
    return out


def _draw_table(c: canvas.Canvas, data: List[List[str]], x: float, y: float, col_widths: List[float]) -> float:
    """Desenha tabela e retorna y final (abaixo da tabela)."""
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), COR_HEADER),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.Color(0.7, 0.7, 0.7)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ]
        )
    )
    w, h = table.wrap(sum(col_widths), 500)
    table.drawOn(c, x, y - h)
    return y - h


def gerar_pdf_bytes(
    fech: EntregadorFechamento,
    chave_pix: Optional[str] = None,
    db: Optional[Session] = None,
) -> bytes:
    """Gera PDF rico (paisagem) quando db é informado; fallback simples sem db."""
    if db is not None:
        try:
            return _gerar_pdf_rico(db, fech, chave_pix=chave_pix)
        except Exception:
            logger.exception(
                "fechamento_pdf_rico_failed id=%s — usando layout simples",
                getattr(fech, "id_fechamento", None),
            )
    return _gerar_pdf_simples(fech, chave_pix=chave_pix)


def _gerar_pdf_simples(fech: EntregadorFechamento, chave_pix: Optional[str] = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 20 * mm
    left = 18 * mm

    def line(txt: str, size: int = 11, gap: float = 6 * mm, bold: bool = False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, txt)
        y -= gap

    codigo = build_fechamento_code(fech)
    line("TrackingSaídas — Fechamento", 16, 8 * mm, bold=True)
    line(f"Código: {codigo}", 12, 6 * mm, bold=True)
    line(f"Status: {_normalize_status(fech.status)}")
    line(f"Executor: {fech.username_entregador or '—'}")
    if chave_pix:
        line(f"PIX: {chave_pix}")
    line(f"Período: {_fmt_date(fech.periodo_inicio)} a {_fmt_date(fech.periodo_fim)}")
    line(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    y -= 4 * mm
    c.setStrokeColorRGB(0.29, 0.18, 0.5)
    c.setLineWidth(1)
    c.line(left, y, width - left, y)
    y -= 8 * mm

    line("Resumo financeiro", 13, 7 * mm, bold=True)
    line(f"Valor base: {_fmt_brl(fech.valor_base)}")
    line(f"Adições: {_fmt_brl(fech.valor_adicao)}")
    if fech.motivo_adicao:
        line(f"  Motivo adição: {fech.motivo_adicao}", 9, 5 * mm)
    line(f"Subtrações: {_fmt_brl(fech.valor_subtracao)}")
    if fech.motivo_subtracao:
        line(f"  Motivo subtração: {fech.motivo_subtracao}", 9, 5 * mm)
    y -= 2 * mm
    line(f"TOTAL A PAGAR: {_fmt_brl(fech.valor_final)}", 14, 8 * mm, bold=True)

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(left, 12 * mm, "Documento gerado automaticamente pelo TrackingSaídas.")
    c.showPage()
    c.save()
    return buf.getvalue()


def _gerar_pdf_rico(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> bytes:
    itens = _collect_itens_diarios(db, fech)
    codigo = build_fechamento_code(fech)
    status = _normalize_status(fech.status)
    ent_nome = fech.username_entregador or "Motoboy"

    sum_shopee = sum(int(r["shopee"]) for r in itens)
    sum_flex = sum(int(r["flex"]) for r in itens)
    sum_avulso = sum(int(r["avulso"]) for r in itens)
    total_feitos = sum_shopee + sum_flex + sum_avulso
    total_cancelados = sum(int(r["total_cancelado"]) for r in itens)
    valor_feitos = sum((r["valor_feitos"] for r in itens), Decimal("0"))
    valor_cancelados = sum((r["valor_cancelados"] for r in itens), Decimal("0"))
    valor_base_calc = (valor_feitos - valor_cancelados).quantize(Decimal("0.01"))
    total_ajustes = Decimal(str(fech.valor_adicao or 0)) - Decimal(str(fech.valor_subtracao or 0))
    total_g = sum(int(r["g_total"]) for r in itens)
    total_g_shopee = sum(int(r["g_shopee"]) for r in itens)
    total_g_flex = sum(int(r["g_flex"]) for r in itens)
    total_g_avulso = sum(int(r["g_avulso"]) for r in itens)

    buf = io.BytesIO()
    page = landscape(A4)
    c = canvas.Canvas(buf, pagesize=page)
    page_w, page_h = page
    M = 10 * mm
    footer_reserved = 14 * mm

    def new_page_header(continuacao: bool = False) -> float:
        if continuacao:
            c.showPage()
        c.setFillColor(COR_HEADER)
        c.rect(0, page_h - 18 * mm, page_w, 18 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 14)
        title = "FECHAMENTO DE ENTREGAS — MOTOBOY"
        if continuacao:
            title += " (continuação)"
        c.drawCentredString(page_w / 2, page_h - 11 * mm, title)
        c.setFillColor(colors.black)
        return page_h - 24 * mm

    def ensure_space(y: float, needed: float) -> float:
        if y - needed >= footer_reserved:
            return y
        return new_page_header(continuacao=True)

    y = new_page_header(False)
    c.setFont("Helvetica", 9)
    c.drawString(
        M,
        y,
        f"Código: {codigo} | Status: {status} | Entregador: {ent_nome}",
    )
    y -= 5 * mm
    if chave_pix:
        c.drawString(M, y, f"Chave PIX: {chave_pix}")
        y -= 5 * mm
    c.drawString(
        M,
        y,
        f"Período: {_fmt_date(fech.periodo_inicio)} a {_fmt_date(fech.periodo_fim)} | "
        f"Geração: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
    )
    y -= 7 * mm

    y = ensure_space(y, 18 * mm)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "Resumo do fechamento")
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    c.drawString(
        M,
        y,
        f"Feitos: {total_feitos} | Cancelados: {total_cancelados} | G: {total_g} | "
        f"Bruto: {_fmt_brl(valor_feitos)} | Canc.: {_fmt_desconto(valor_cancelados)} | "
        f"Ajustes: {_fmt_signed(total_ajustes)}",
    )
    y -= 5 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(M, y, f"TOTAL A PAGAR: {_fmt_brl(fech.valor_final)}")
    y -= 8 * mm

    y = ensure_space(y, 40 * mm)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "Resumo Financeiro")
    y -= 3 * mm
    fin_rows = [
        ["Descrição", "Valor"],
        ["Valor bruto das entregas", _fmt_brl(valor_feitos)],
        ["Desconto por cancelamentos", _fmt_desconto(valor_cancelados)],
        ["Valor base", _fmt_brl(valor_base_calc)],
        ["Ajustes manuais", _fmt_signed(total_ajustes)],
        ["TOTAL A PAGAR", _fmt_brl(fech.valor_final)],
    ]
    usable_w = page_w - 2 * M
    y = _draw_table(c, fin_rows, M, y, [usable_w * 0.7, usable_w * 0.3])
    y -= 6 * mm

    y = ensure_space(y, 30 * mm)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "Detalhamento por dia")
    y -= 3 * mm
    diaria = [
        [
            "Data",
            "Flex",
            "Shopee",
            "Avulso",
            "G",
            "Feitos",
            "Canc.",
            "Valor feitos",
            "Valor canc.",
            "Total",
        ]
    ]
    for r in itens:
        diaria.append(
            [
                _fmt_date(r["data"]),
                str(r["flex"]),
                str(r["shopee"]),
                str(r["avulso"]),
                str(r["g_total"]),
                str(r["total_feitos"]),
                str(r["total_cancelado"]),
                _fmt_brl(r["valor_feitos"]),
                _fmt_desconto(r["valor_cancelados"]),
                _fmt_brl(r["valor_total"]),
            ]
        )
    if len(diaria) == 1:
        diaria.append(["—", "0", "0", "0", "0", "0", "0", _fmt_brl(0), _fmt_brl(0), _fmt_brl(0)])
    col_w = usable_w / 10
    # Desenhar em fatias se muitas linhas
    chunk_size = 18
    body = diaria[1:]
    header = diaria[0]
    for i in range(0, max(len(body), 1), chunk_size):
        chunk = body[i : i + chunk_size] or [["—", "0", "0", "0", "0", "0", "0", _fmt_brl(0), _fmt_brl(0), _fmt_brl(0)]]
        needed = 8 * mm + len(chunk) * 5 * mm
        y = ensure_space(y, needed)
        y = _draw_table(c, [header] + chunk, M, y, [col_w] * 10)
        y -= 5 * mm

    y = ensure_space(y, 25 * mm)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "Ajustes Manuais")
    y -= 3 * mm
    ajustes: List[List[str]] = [["Tipo", "Justificativa", "Valor"]]
    if Decimal(str(fech.valor_adicao or 0)) > 0:
        ajustes.append(
            ["Acréscimo", fech.motivo_adicao or "—", _fmt_signed(fech.valor_adicao)]
        )
    if Decimal(str(fech.valor_subtracao or 0)) > 0:
        ajustes.append(
            [
                "Desconto",
                fech.motivo_subtracao or "—",
                _fmt_signed(-abs(Decimal(str(fech.valor_subtracao or 0)))),
            ]
        )
    if len(ajustes) == 1:
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.drawString(M, y, "Nenhum ajuste manual aplicado.")
        c.setFillColor(colors.black)
        y -= 6 * mm
    else:
        y = _draw_table(c, ajustes, M, y, [usable_w * 0.2, usable_w * 0.55, usable_w * 0.25])
        y -= 6 * mm

    if total_g > 0:
        y = ensure_space(y, 30 * mm)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(M, y, "Pacotes Grandes")
        y -= 3 * mm
        g_rows = [
            ["Serviço", "Quantidade G"],
            ["Shopee", str(total_g_shopee)],
            ["Flex", str(total_g_flex)],
            ["Avulso", str(total_g_avulso)],
            ["Total", str(total_g)],
        ]
        y = _draw_table(c, g_rows, M, y, [usable_w * 0.7, usable_w * 0.3])
        y -= 6 * mm

    y = ensure_space(y, 22 * mm)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, "Critério de cálculo")
    y -= 5 * mm
    c.setFont("Helvetica", 8)
    for txt in (
        "Valor base = valor bruto das entregas - cancelamentos.",
        "Total a pagar = valor base + ajustes manuais + adicionais aplicáveis.",
        "Pacotes grandes são identificados pela coluna G e seguem a regra vigente.",
    ):
        c.drawString(M, y, txt)
        y -= 4 * mm

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawCentredString(page_w / 2, 10 * mm, "Documento gerado digitalmente pelo sistema.")
    c.drawCentredString(
        page_w / 2,
        6 * mm,
        f"Código do fechamento: {codigo} · Status: {status}",
    )
    c.drawCentredString(
        page_w / 2,
        2.5 * mm,
        f"Data de geração: {datetime.now().strftime('%d/%m/%Y %H:%M')} · "
        f"{datetime.now().year} © TrackingSaídas.",
    )
    c.showPage()
    c.save()
    return buf.getvalue()


def upload_fechamento_pdf(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> Optional[str]:
    """Gera PDF, sobe ao B2 e atualiza colunas do fechamento. Retorna object_key ou None.

    Usa prefixo `saida/fechamento/...` porque a application key do B2 em produção
    costuma estar restrita ao prefixo `saida/` (fotos). Prefixo `fechamento/` gera AccessDenied.
    """
    pdf_bytes = gerar_pdf_bytes(fech, chave_pix=chave_pix, db=db)
    codigo = build_fechamento_code(fech)
    # Preferir saida/ (entitlement tipico do B2); manter legado fechamento/ como fallback de leitura
    object_key = (
        f"saida/fechamento/{fech.sub_base}/{fech.id_fechamento}/fechamento_{codigo}.pdf"
    )

    client = get_s3_client_optional()
    uploaded = False
    if client and b2_configured():
        try:
            client.put_object(
                Bucket=B2_BUCKET_NAME,
                Key=object_key,
                Body=pdf_bytes,
                ContentType="application/pdf",
            )
            uploaded = True
        except Exception as e:
            err_name = type(e).__name__
            logger.exception(
                "fechamento_pdf_upload_failed id=%s bucket=%s key=%s err=%s — "
                "verifique B2_BUCKET_NAME e permissao write no prefixo saida/",
                fech.id_fechamento,
                B2_BUCKET_NAME,
                object_key,
                err_name,
            )
    else:
        logger.warning("b2_not_configured_fechamento_pdf id=%s", fech.id_fechamento)

    # Só grava key se o upload realmente funcionou (download regenera sob demanda)
    if uploaded:
        fech.pdf_object_key = object_key
        fech.pdf_gerado_em = datetime.utcnow()
        db.flush()
        return object_key

    fech.pdf_gerado_em = datetime.utcnow()
    db.flush()
    return None


def get_fechamento_pdf_bytes(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> bytes:
    """Sempre regenera o PDF rico para garantir paridade web/mobile."""
    return gerar_pdf_bytes(fech, chave_pix=chave_pix, db=db)
