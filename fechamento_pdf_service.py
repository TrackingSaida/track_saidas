"""
Gera PDF de fechamento de motoboy/entregador (reportlab) e grava no B2.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from models import EntregadorFechamento
from upload_storage_utils import B2_BUCKET_NAME, b2_configured, get_s3_client_optional

logger = logging.getLogger(__name__)


def _fmt_brl(v) -> str:
    try:
        n = Decimal(str(v or 0))
    except Exception:
        n = Decimal("0")
    s = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _fmt_date(d) -> str:
    if not d:
        return "—"
    try:
        return d.strftime("%d/%m/%Y")
    except Exception:
        return str(d)


def build_fechamento_code(fech: EntregadorFechamento) -> str:
    periodo = fech.periodo_fim or fech.periodo_inicio
    key = periodo.strftime("%Y%m") if periodo else "000000"
    raw = (fech.username_entregador or "MOTOBOY").upper()
    raw = re.sub(r"[^A-Z0-9]+", "", raw)[:12] or "MOTOBOY"
    return f"FEC-{key}-{raw}-{int(fech.id_fechamento):06d}"


def gerar_pdf_bytes(fech: EntregadorFechamento, chave_pix: Optional[str] = None) -> bytes:
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
    line(f"Status: {(fech.status or '').upper()}")
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

    y -= 6 * mm
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(left, 12 * mm, "Documento gerado automaticamente pelo TrackingSaídas.")
    c.showPage()
    c.save()
    return buf.getvalue()


def upload_fechamento_pdf(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> Optional[str]:
    """Gera PDF, sobe ao B2 e atualiza colunas do fechamento. Retorna object_key ou None."""
    pdf_bytes = gerar_pdf_bytes(fech, chave_pix=chave_pix)
    codigo = build_fechamento_code(fech)
    object_key = f"fechamento/{fech.sub_base}/{fech.id_fechamento}/fechamento_{codigo}.pdf"

    client = get_s3_client_optional()
    if client and b2_configured():
        try:
            client.put_object(
                Bucket=B2_BUCKET_NAME,
                Key=object_key,
                Body=pdf_bytes,
                ContentType="application/pdf",
            )
        except Exception:
            logger.exception("fechamento_pdf_upload_failed id=%s", fech.id_fechamento)
            # Ainda assim mantém key lógica; download pode regenerar
    else:
        logger.warning("b2_not_configured_fechamento_pdf id=%s", fech.id_fechamento)

    fech.pdf_object_key = object_key
    fech.pdf_gerado_em = datetime.utcnow()
    db.flush()
    return object_key


def get_fechamento_pdf_bytes(
    db: Session,
    fech: EntregadorFechamento,
    *,
    chave_pix: Optional[str] = None,
) -> bytes:
    """Baixa do B2 se possível; senão regenera sob demanda."""
    client = get_s3_client_optional()
    key = (fech.pdf_object_key or "").strip()
    if client and key and b2_configured():
        try:
            obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=key)
            return obj["Body"].read()
        except Exception:
            logger.warning("fechamento_pdf_get_failed key=%s — regenerando", key)
    return gerar_pdf_bytes(fech, chave_pix=chave_pix)
