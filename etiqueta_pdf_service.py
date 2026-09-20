"""Geração modular de etiquetas PDF/PNG (código existente + envio próprio)."""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from etiqueta_identidade_service import (
    LOGO_ORIGEM_ROTEVO,
    fit_logo_image,
    resolver_contato,
    resolver_logo_etiqueta,
    resolver_nome_exibicao,
    resolver_slogan,
)
from models import Owner

logger = logging.getLogger(__name__)


def _gerar_pdf_etiqueta_codigo(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
) -> bytes:
    """Layout legado (POST /etiquetas/gerar) — QR dominante, visual preservado."""
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import qrcode

    dados = dados_extras or {}
    largura_pag = 100 * mm
    altura_pag = 150 * mm
    margin = 8 * mm
    area_util_w = largura_pag - 2 * margin
    qr_size = 60 * mm

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura_pag, altura_pag))
    c.setPageSize((largura_pag, altura_pag))

    def center_x(elem_width: float) -> float:
        return (largura_pag - elem_width) / 2

    y = altura_pag - margin
    titulo = "ROTEVO"
    if modo_final == "shopee":
        titulo = "SHOPEE ENTREGA"
    elif modo_final == "ml":
        titulo = "MERCADO ENVIOS"

    c.setFont("Helvetica", 8)
    tw = c.stringWidth(titulo, "Helvetica", 8)
    c.drawString(center_x(tw), y, titulo)
    y -= 6 * mm

    qr_x = center_x(qr_size)
    qr_y = y - qr_size
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=1,
    )
    qr.add_data(qr_content if qr_content else codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    c.drawImage(ImageReader(qr_buf), qr_x, qr_y, width=qr_size, height=qr_size)
    y = qr_y - 4 * mm

    c.setFont("Helvetica-Bold", 14)
    tw = c.stringWidth(codigo, "Helvetica-Bold", 14)
    if tw > area_util_w:
        c.setFont("Helvetica-Bold", 10)
        tw = c.stringWidth(codigo, "Helvetica-Bold", 10)
    c.drawString(center_x(tw), y, codigo)
    y -= 8 * mm

    dest = dados.get("destinatario") or ""
    cidade = dados.get("cidade") or ""
    cep = dados.get("cep") or ""
    if dest or cidade or cep:
        c.setFont("Helvetica", 7)
        linhas = []
        if dest:
            linhas.append(str(dest)[:40])
        if cidade or cep:
            linhas.append(f"{cidade} {cep}".strip()[:40])
        for linha in linhas[:3]:
            if linha:
                c.drawString(margin, y, linha)
                y -= 4 * mm

    rodape = "ROTEVO"
    c.setFont("Helvetica", 6)
    rw = c.stringWidth(rodape, "Helvetica", 6)
    c.drawString(center_x(rw), margin, rodape)
    c.save()
    buf.seek(0)
    return buf.getvalue()


def _gerar_png_etiqueta_codigo(
    codigo: str,
    modo_final: str,
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    import qrcode

    largura, altura = 1181, 1772
    margem = 94
    qr_size = 708
    img = Image.new("RGB", (largura, altura), "white")
    draw = ImageDraw.Draw(img)

    def _font(size: int, bold: bool = False):
        try:
            if bold:
                return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size=size)
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size=size)
        except Exception:
            return ImageFont.load_default()

    def _center_x(text: str, font) -> int:
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        return int((largura - (right - left)) / 2)

    titulo = "ROTEVO"
    if modo_final == "shopee":
        titulo = "SHOPEE ENTREGA"
    elif modo_final == "ml":
        titulo = "MERCADO ENVIOS"

    y = margem
    font_titulo = _font(28)
    draw.text((_center_x(titulo, font_titulo), y), titulo, fill="black", font=font_titulo)
    y += 72

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=1,
    )
    qr.add_data(qr_content if qr_content else codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((qr_size, qr_size))
    img.paste(qr_img, (int((largura - qr_size) / 2), y))
    y += qr_size + 45

    font_codigo = _font(58, bold=True)
    draw.text((_center_x(codigo, font_codigo), y), codigo, fill="black", font=font_codigo)
    y += 84

    dados = dados_extras or {}
    dest = str(dados.get("destinatario") or "").strip()
    cidade = str(dados.get("cidade") or "").strip()
    cep = str(dados.get("cep") or "").strip()
    if dest or cidade or cep:
        font_info = _font(24)
        if dest:
            draw.text((margem, y), dest[:40], fill="black", font=font_info)
            y += 36
        if cidade or cep:
            draw.text((margem, y), f"{cidade} {cep}".strip()[:40], fill="black", font=font_info)

    font_rodape = _font(20)
    draw.text((_center_x("ROTEVO", font_rodape), altura - margem), "ROTEVO", fill="black", font=font_rodape)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.getvalue()


def _fmt_cep(cep: Optional[str]) -> str:
    digits = "".join(c for c in (cep or "") if c.isdigit())
    if len(digits) == 8:
        return f"{digits[:5]}-{digits[5:]}"
    return (cep or "").strip() or "—"


def _fmt_tel(tel: Optional[str]) -> str:
    digits = "".join(c for c in (tel or "") if c.isdigit())
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return (tel or "").strip() or "—"


def _fmt_contato(raw: Optional[str]) -> str:
    digits = "".join(c for c in (raw or "") if c.isdigit())
    if len(digits) in (10, 11):
        formatted = _fmt_tel(digits)
        return "" if formatted == "—" else formatted
    return (raw or "").strip()


def _clip(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max(0, max_len - 1)] + "…"


def _endereco_linha(
    *,
    rua: str,
    numero: str,
    complemento: Optional[str],
    bairro: str,
    cidade: str,
    uf: str,
) -> str:
    parts = [f"{rua}, {numero}".strip(", ")]
    if complemento:
        parts[0] = f"{parts[0]} — {complemento}"
    if bairro:
        parts.append(bairro)
    cidade_uf = f"{cidade} / {uf}".strip(" /")
    if cidade_uf:
        parts.append(cidade_uf)
    return ", ".join(p for p in parts if p)


def _wrap_lines(c, text: str, font: str, size: float, max_width: float, max_lines: int = 3) -> list[str]:
    words = (text or "").split()
    if not words:
        return []
    lines: list[str] = []
    cur = ""
    extra = False
    for w in words:
        trial = f"{cur} {w}".strip()
        if c.stringWidth(trial, font, size) <= max_width or not cur:
            cur = trial
            continue
        lines.append(cur)
        cur = w
        if len(lines) >= max_lines:
            extra = True
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    elif cur:
        extra = True
    if extra and lines:
        lines[-1] = _clip(lines[-1], max(8, len(lines[-1])))
    return lines[:max_lines]


def _draw_phone_icon(c, x: float, y: float, size: float, color) -> None:
    """Ícone de telefone (Material), caixa size x size, origem inferior-esquerda."""
    c.saveState()
    c.translate(x, y)
    s = size / 24.0
    c.scale(s, s)
    c.setFillColor(color)
    p = c.beginPath()

    def pt(px: float, py: float):
        return px, 24.0 - py

    p.moveTo(*pt(6.62, 10.79))
    p.curveTo(*pt(8.06, 13.62), *pt(10.38, 15.94), *pt(13.21, 17.38))
    p.lineTo(*pt(15.41, 15.18))
    p.curveTo(*pt(15.69, 14.9), *pt(16.08, 14.82), *pt(16.43, 14.93))
    p.curveTo(*pt(17.55, 15.3), *pt(18.75, 15.5), *pt(20, 15.5))
    p.curveTo(*pt(20.55, 15.5), *pt(21, 15.95), *pt(21, 16.5))
    p.lineTo(*pt(21, 20))
    p.curveTo(*pt(21, 20.55), *pt(20.55, 21), *pt(20, 21))
    p.curveTo(*pt(10.61, 21), *pt(3, 13.39), *pt(3, 4))
    p.curveTo(*pt(3, 3.45), *pt(3.45, 3), *pt(4, 3))
    p.lineTo(*pt(7.5, 3))
    p.curveTo(*pt(8.05, 3), *pt(8.5, 3.45), *pt(8.5, 4))
    p.curveTo(*pt(8.5, 5.25), *pt(8.7, 6.45), *pt(9.07, 7.57))
    p.curveTo(*pt(9.18, 7.92), *pt(9.1, 8.31), *pt(8.82, 8.59))
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.restoreState()


def gerar_pdf_envio_proprio(
    *,
    owner: Optional[Owner],
    codigo: str,
    remetente: Dict[str, Any],
    destinatario: Dict[str, Any],
    peso_kg: Optional[float] = None,
    dimensoes: Optional[str] = None,
    created_at: Optional[datetime] = None,
    nome_exibicao_override: Optional[str] = None,
    slogan_override: Optional[str] = None,
    logo_key_hint: Optional[str] = None,
    observacao: Optional[str] = None,
    contato_override: Optional[str] = None,
    pedido_loja: Optional[str] = None,
) -> bytes:
    """
    Layout mockup 100x150mm: header Owner, código+QR (+pedido loja),
    destinatário (destaque), remetente (menor), stats, rodapé.
    """
    from reportlab.lib.colors import Color, black, white, HexColor
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    import qrcode

    _ = logo_key_hint

    largura = 100 * mm
    altura = 150 * mm
    margin = 4.5 * mm
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(largura, altura))

    gray = HexColor("#6B7280")
    dark = HexColor("#111827")
    line = HexColor("#D1D5DB")
    badge_bg = HexColor("#374151")
    footer_bg = HexColor("#1F2937")

    nome = (nome_exibicao_override or resolver_nome_exibicao(owner)).strip()
    slogan = (slogan_override if slogan_override is not None else resolver_slogan(owner)).strip()
    contato_txt = _fmt_contato(
        contato_override if contato_override is not None else resolver_contato(owner)
    )
    logo_bytes, logo_origem = resolver_logo_etiqueta(owner)

    c.setStrokeColor(line)
    c.setLineWidth(1)
    c.roundRect(2 * mm, 2 * mm, largura - 4 * mm, altura - 4 * mm, 3 * mm, stroke=1, fill=0)

    y = altura - margin - 2 * mm

    header_h = 26 * mm if contato_txt else 22 * mm
    header_top = y
    contact_row = 5.8 * mm if contato_txt else 0
    logo_max_w = 34 * mm
    logo_max_h = max(14 * mm, header_h - contact_row - 0.6 * mm)
    draw_w = draw_h = 0
    if logo_bytes:
        fitted = fit_logo_image(logo_bytes, max_width=900, max_height=520)
        if fitted is not None:
            logo_buf = io.BytesIO()
            fitted.save(logo_buf, format="PNG")
            logo_buf.seek(0)
            aspect = fitted.width / max(1.0, float(fitted.height))
            if (logo_max_w / logo_max_h) > aspect:
                draw_h = logo_max_h
                draw_w = draw_h * aspect
            else:
                draw_w = logo_max_w
                draw_h = draw_w / aspect
            c.drawImage(
                ImageReader(logo_buf),
                margin,
                header_top - draw_h,
                width=draw_w,
                height=draw_h,
                mask="auto",
                preserveAspectRatio=True,
            )
            text_x = margin + draw_w + 2.6 * mm
        else:
            text_x = margin
    else:
        text_x = margin

    show_name = True
    if logo_origem == LOGO_ORIGEM_ROTEVO and nome.strip().upper() in {"ROTEVO", "ROTEVO TECNOLOGIA"}:
        show_name = False

    text_right = largura - margin
    text_width = max(10 * mm, text_right - text_x)
    name_font_size = 13
    slogan_font_size = 8
    logo_band = draw_h if draw_h else 16 * mm
    mid = header_top - logo_band / 2.0

    if show_name and slogan:
        name_y = mid + 2.4 * mm
        slogan_y = mid - 2.6 * mm
    elif show_name:
        name_y = mid - 1.6 * mm
        slogan_y = None
    elif slogan:
        name_y = None
        slogan_y = mid - 1.2 * mm
    else:
        name_y = slogan_y = None

    if show_name and name_y is not None:
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", name_font_size)
        max_chars = max(12, int(text_width / (name_font_size * 0.52)))
        c.drawString(text_x, name_y, _clip(nome.upper(), max_chars))

    if slogan and slogan_y is not None:
        c.setFillColor(gray)
        c.setFont("Helvetica", slogan_font_size)
        max_chars_slogan = max(16, int(text_width / (slogan_font_size * 0.48)))
        c.drawString(text_x, slogan_y, _clip(slogan, max_chars_slogan))

    if contato_txt:
        icon_size = 4.2 * mm
        contact_baseline = header_top - header_h + 2.4 * mm
        contact_color = HexColor("#374151")
        _draw_phone_icon(c, margin, contact_baseline - 0.6 * mm, icon_size, contact_color)
        c.setFillColor(contact_color)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin + icon_size + 1.4 * mm, contact_baseline, _clip(contato_txt, 24))

    y = header_top - header_h
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 4 * mm

    qr_size = 26 * mm
    c.setFillColor(gray)
    c.setFont("Helvetica", 7)
    c.drawString(margin, y, "CÓDIGO DO PEDIDO")
    y -= 5 * mm
    c.setFillColor(dark)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, _clip(codigo, 22))
    y_codigo = y
    y -= 4.2 * mm

    pedido_txt = (pedido_loja or "").strip()
    if pedido_txt:
        c.setFillColor(gray)
        c.setFont("Helvetica", 6)
        c.drawString(margin, y, "PEDIDO LOJA")
        y -= 3.4 * mm
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, y, _clip(pedido_txt, 28))
        y -= 3.6 * mm

    c.setFillColor(gray)
    c.setFont("Helvetica", 6)
    c.drawString(margin, y, "QR CODE DE IDENTIFICAÇÃO DO ENVIO")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=1,
    )
    qr.add_data(codigo)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    qr_x = largura - margin - qr_size
    qr_y = y_codigo - qr_size + 4 * mm
    c.drawImage(ImageReader(qr_buf), qr_x, qr_y, width=qr_size, height=qr_size)

    obs = (observacao or "").strip()
    if obs:
        y -= 6.5 * mm
        c.setFillColor(gray)
        c.setFont("Helvetica", 6)
        c.drawString(margin, y, "OBSERVAÇÃO")
        y -= 3.4 * mm
        c.setFillColor(dark)
        c.setFont("Helvetica", 7)
        obs_width = max(20 * mm, qr_x - margin - 2.5 * mm)
        for obs_line in _wrap_lines(c, obs, "Helvetica", 7, obs_width, max_lines=2):
            c.drawString(margin, y, obs_line)
            y -= 3.2 * mm
        y += 1.0 * mm

    y = min(y, qr_y) - 3 * mm
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 4 * mm

    def _draw_party_block(
        title: str,
        person: Dict[str, Any],
        y_top: float,
        *,
        prominent: bool,
    ) -> float:
        badge_w = 32 * mm if prominent else 24 * mm
        badge_h = 5.2 * mm if prominent else 4.2 * mm
        name_size = 11 if prominent else 7.5
        addr_size = 8 if prominent else 6.5
        meta_size = 7 if prominent else 6
        line_gap = 3.6 * mm if prominent else 2.8 * mm
        name_gap = 4.5 * mm if prominent else 3.2 * mm

        c.setFillColor(badge_bg)
        c.roundRect(margin, y_top - badge_h + 1 * mm, badge_w, badge_h, 2 * mm, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 7 if prominent else 6)
        c.drawCentredString(margin + badge_w / 2, y_top - badge_h / 2 - 0.8 * mm, title)
        yy = y_top - badge_h - 3.2 * mm
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", name_size)
        c.drawString(margin, yy, _clip(str(person.get("nome") or "").upper(), 40 if prominent else 48))
        yy -= name_gap
        c.setFont("Helvetica", addr_size)
        c.setFillColor(dark)
        endereco = _endereco_linha(
            rua=str(person.get("rua") or ""),
            numero=str(person.get("numero") or ""),
            complemento=person.get("complemento"),
            bairro=str(person.get("bairro") or ""),
            cidade=str(person.get("cidade") or ""),
            uf=str(person.get("uf") or ""),
        )
        max_w = largura - 2 * margin
        words = endereco.split()
        line_cur = ""
        max_addr_lines = 3 if prominent else 2
        lines_drawn = 0
        for w in words:
            trial = f"{line_cur} {w}".strip()
            if c.stringWidth(trial, "Helvetica", addr_size) > max_w and line_cur:
                c.drawString(margin, yy, line_cur)
                yy -= line_gap
                lines_drawn += 1
                line_cur = w
                if lines_drawn >= max_addr_lines:
                    line_cur = ""
                    break
            else:
                line_cur = trial
        if line_cur and lines_drawn < max_addr_lines:
            c.drawString(margin, yy, _clip(line_cur, 70))
            yy -= line_gap
        c.setFillColor(gray)
        c.setFont("Helvetica", meta_size)
        c.drawString(margin, yy, f"CEP {_fmt_cep(person.get('cep'))}")
        yy -= line_gap
        if person.get("telefone"):
            c.drawString(margin, yy, _fmt_tel(person.get("telefone")))
            yy -= line_gap
        return yy

    y = _draw_party_block("DESTINATÁRIO", destinatario, y, prominent=True)
    y -= 1.5 * mm
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 3.5 * mm
    y = _draw_party_block("REMETENTE", remetente, y, prominent=False)
    y -= 1.5 * mm
    c.setStrokeColor(line)
    c.line(margin, y, largura - margin, y)
    y -= 3 * mm

    col_w = (largura - 2 * margin) / 3
    peso_txt = f"{peso_kg:g} kg".replace(".", ",") if peso_kg is not None else "—"
    dim_txt = (dimensoes or "").strip() or "—"
    data_txt = (created_at or datetime.utcnow()).strftime("%d/%m/%Y")
    stats = [("PESO", peso_txt), ("DIMENSÕES", dim_txt), ("DATA DE ENVIO", data_txt)]
    for i, (label, value) in enumerate(stats):
        x = margin + i * col_w
        c.setFillColor(gray)
        c.setFont("Helvetica", 6)
        c.drawString(x + 1 * mm, y, label)
        c.setFillColor(dark)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 1 * mm, y - 4 * mm, _clip(value, 16))
        if i < 2:
            c.setStrokeColor(line)
            c.line(x + col_w, y + 2 * mm, x + col_w, y - 6 * mm)
    y -= 10 * mm

    footer_h = 9 * mm
    c.setFillColor(footer_bg)
    c.rect(2 * mm, 2 * mm, largura - 4 * mm, footer_h, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(largura / 2, 2 * mm + footer_h / 2 - 1.1 * mm, "OBRIGADO PELA SUA CONFIANÇA!")

    c.save()
    buf.seek(0)
    return buf.getvalue()



def gerar_etiqueta(
    *,
    modo: str = "codigo_existente",
    codigo: str,
    formato: str = "pdf",
    modo_final: str = "generic",
    dados_extras: Optional[Dict[str, Any]] = None,
    qr_content: Optional[str] = None,
    owner: Optional[Owner] = None,
    remetente: Optional[Dict[str, Any]] = None,
    destinatario: Optional[Dict[str, Any]] = None,
    peso_kg: Optional[float] = None,
    dimensoes: Optional[str] = None,
    created_at: Optional[datetime] = None,
    nome_exibicao_override: Optional[str] = None,
    slogan_override: Optional[str] = None,
    logo_key_hint: Optional[str] = None,
    observacao: Optional[str] = None,
    contato_override: Optional[str] = None,
    pedido_loja: Optional[str] = None,
) -> bytes:
    formato = (formato or "pdf").strip().lower()
    if modo == "envio_proprio":
        if formato != "pdf":
            # MVP: envio próprio só PDF
            formato = "pdf"
        return gerar_pdf_envio_proprio(
            owner=owner,
            codigo=codigo,
            remetente=remetente or {},
            destinatario=destinatario or {},
            peso_kg=peso_kg,
            dimensoes=dimensoes,
            created_at=created_at,
            nome_exibicao_override=nome_exibicao_override,
            slogan_override=slogan_override,
            logo_key_hint=logo_key_hint,
            observacao=observacao,
            contato_override=contato_override,
            pedido_loja=pedido_loja,
        )
    if formato == "png":
        return _gerar_png_etiqueta_codigo(codigo, modo_final, dados_extras, qr_content)
    return _gerar_pdf_etiqueta_codigo(codigo, modo_final, dados_extras, qr_content)
