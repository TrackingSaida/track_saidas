"""
Rotas de upload: presigned PUT (B2) para o mobile enviar foto direto;
presigned GET para o web exibir imagens do bucket privado.
Prefixo: /upload. Auth: get_current_user (web e mobile).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_LOGO_PATH = _BASE_DIR / "assets" / "logo-comprovante.png"

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session
from sqlalchemy import select, func

from auth import get_current_user
from db import get_db
from models import User, Saida, SaidaDetail, SaidaHistorico, Motoboy
from upload_storage_utils import (
    B2_BUCKET_NAME,
    extract_foto_keys,
    extract_object_key,
    get_s3_client_optional,
    parse_foto_items,
    parse_id_saida_from_object_key,
)

OPERACAO_TZ = ZoneInfo("America/Sao_Paulo")

router = APIRouter(prefix="/upload", tags=["Upload - Fotos entrega"])


def _get_s3_client():
    client = get_s3_client_optional()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Upload não configurado (B2 credentials ausentes).",
        )
    return client


def _ensure_saida_owned(db, sub_base: str, id_saida: int) -> Saida:
    s = db.get(Saida, id_saida)
    if not s or s.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Saída não encontrada.")
    return s


def _ensure_motoboy_owns_saida(current_user: User, saida: Saida) -> None:
    role = getattr(current_user, "role", None)
    if role != 4:
        return
    motoboy_id = getattr(current_user, "motoboy_id", None)
    if not motoboy_id or int(saida.motoboy_id or 0) != int(motoboy_id):
        status = str(saida.status or "").strip().upper()
        motivo = (
            "CANCELADO"
            if status == "CANCELADO"
            else "DESATRIBUIDO"
            if saida.motoboy_id is None
            else "REATRIBUIDO"
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACAO_PEDIDO_OBSOLETA",
                "reason": motivo,
                "message": "O pedido não pertence mais a este motoboy.",
            },
        )


def _is_pending_object_key(object_key: str) -> bool:
    """Keys de foto tirada antes de existir a saída (ex.: lançar avulso)."""
    return (object_key or "").startswith("saida/pending/")


def _ensure_pending_object_key_owned(
    db: Session,
    sub_base: str,
    object_key: str,
    *,
    id_saida_hint: Optional[int] = None,
) -> None:
    """
    Fotos de avulso sobem como saida/pending/... e depois são referenciadas em saidas_detail.
    Sem id_saida no path, valida pelo vínculo na sub_base (e id_saida opcional do cliente).
    """
    key = (object_key or "").strip()
    if not _is_pending_object_key(key):
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")

    if id_saida_hint is not None:
        _ensure_saida_owned(db, sub_base, id_saida_hint)
        rows = db.execute(
            select(SaidaDetail.foto_url).where(SaidaDetail.id_saida == int(id_saida_hint))
        ).scalars().all()
        for foto_url in rows:
            if key in extract_foto_keys(foto_url):
                return
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")

    # Escape LIKE wildcards no object_key (uuid hex, mas defensivo).
    like_pat = (
        key.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    found = db.execute(
        select(Saida.id_saida)
        .join(SaidaDetail, SaidaDetail.id_saida == Saida.id_saida)
        .where(Saida.sub_base == sub_base)
        .where(SaidaDetail.foto_url.is_not(None))
        .where(SaidaDetail.foto_url.like(f"%{like_pat}%", escape="\\"))
        .limit(1)
    ).first()
    if not found:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")


def _ensure_object_key_owned(
    db: Session,
    sub_base: str,
    object_key: str,
    *,
    id_saida_hint: Optional[int] = None,
) -> None:
    id_saida = parse_id_saida_from_object_key(object_key)
    if id_saida is not None:
        _ensure_saida_owned(db, sub_base, id_saida)
        return
    if _is_pending_object_key(object_key):
        _ensure_pending_object_key_owned(
            db, sub_base, object_key, id_saida_hint=id_saida_hint
        )
        return
    raise HTTPException(status_code=404, detail="Comprovante não encontrado.")


# ---------- Schemas ----------


class PresignIn(BaseModel):
    filename: str = Field(min_length=1)
    # id_saida opcional só para tipo=lancar_avulso (foto antes de criar a saída).
    id_saida: Optional[int] = Field(default=None, gt=0)
    tipo: str = Field(pattern="^(entregue|ausente|devolucao|lancar_avulso)$")
    content_type: str = Field(default="image/jpeg")
    photo_id: Optional[str] = Field(default=None, max_length=80)


class PresignGetIn(BaseModel):
    foto_url: Optional[str] = None
    foto_urls: Optional[List[str]] = None
    # Contexto opcional para keys saida/pending/... (foto de avulso antes do id_saida).
    id_saida: Optional[int] = Field(default=None, gt=0)


class WatermarkFotoOut(BaseModel):
    tem_comprovante: bool
    image_count: int = 0
    image_url: Optional[str] = None


class ComprovanteExportIn(BaseModel):
    index: int = Field(default=0, ge=0)


def _build_watermark_image_bytes(*, image_bytes: bytes, codigo: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    codigo_txt = (codigo or "").strip() or "SEM_CODIGO"
    with Image.open(BytesIO(image_bytes)) as img:
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_size = max(24, int(min(img.size) * 0.06))
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size=font_size)
        except Exception:
            font = ImageFont.load_default()

        text_w, text_h = draw.textbbox((0, 0), codigo_txt, font=font)[2:]
        x = max(12, img.size[0] - text_w - 18)
        y = max(12, img.size[1] - text_h - 18)
        pad = 8
        draw.rectangle(
            [(x - pad, y - pad), (x + text_w + pad, y + text_h + pad)],
            fill=(0, 0, 0, 110),
        )
        draw.text((x, y), codigo_txt, font=font, fill=(255, 255, 255, 210))

        out = Image.alpha_composite(img, overlay).convert("RGB")
        buf = BytesIO()
        out.save(buf, format="JPEG", quality=88, optimize=True)
        buf.seek(0)
        return buf.getvalue()


def _status_amigavel(status: Optional[str]) -> str:
    norm = (status or "").strip().upper().replace(" ", "_")
    mapping = {
        "ENTREGUE": "Entregue",
        "AUSENTE": "Ausente",
        "EM_ROTA": "Em rota",
        "SAIU_PARA_ENTREGA": "Saiu para entrega",
        "CANCELADO": "Cancelado",
    }
    return mapping.get(norm, (status or "—").strip() or "—")


def _is_status_ocorrencia_label(status_label: Optional[str]) -> bool:
    """Ausente na homol; também aceita o rótulo Ocorrência."""
    s = (status_label or "").strip().lower()
    return s in {"ausente", "ocorrência", "ocorrencia"}


def _format_dt_br(value: Optional[datetime], *, assume_utc: bool = False) -> str:
    """Formata data/hora no fuso operacional (America/Sao_Paulo).

    Timestamps de `saida_historico` vêm de NOW() do Postgres em TIMESTAMP WITHOUT
    TIME ZONE e são horário de parede da operação — iguais aos exibidos na timeline.
    Já `data_hora_entrega` legada é gravada com datetime.utcnow(); nesse fallback
    use assume_utc=True.
    """
    if not value:
        return ""
    try:
        dt = value
        if dt.tzinfo is None:
            if assume_utc:
                dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(OPERACAO_TZ)
            else:
                dt = dt.replace(tzinfo=OPERACAO_TZ)
        else:
            dt = dt.astimezone(OPERACAO_TZ)
        return dt.strftime("%d/%m/%Y às %H:%M")
    except Exception:
        return str(value)


def _load_detail_for_saida(db: Session, id_saida: int) -> Optional[SaidaDetail]:
    return db.execute(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    ).scalar_one_or_none()


def _ultima_data_ocorrencia(db: Session, saida: Saida) -> tuple[Optional[datetime], bool]:
    """Retorna (datetime, assume_utc) da última ocorrência relevante.

    Prefere o timestamp do histórico (mesma fonte da timeline). Fallback legado
    para data_hora_entrega (UTC naive).
    """
    status_norm = (saida.status or "").strip().upper().replace(" ", "_")
    eventos = (
        ("entregue", "entregue_lote")
        if status_norm == "ENTREGUE"
        else ("ausente", "ausente_lote", "entregue", "ausente")
    )
    hist_ts = db.execute(
        select(func.max(SaidaHistorico.timestamp)).where(
            SaidaHistorico.id_saida == saida.id_saida,
            SaidaHistorico.evento.in_(eventos),
        )
    ).scalar_one_or_none()
    if hist_ts is not None:
        return hist_ts, False
    if saida.data_hora_entrega:
        return saida.data_hora_entrega, True
    return None, False


def _primeiro_nome(nome_completo: Optional[str]) -> str:
    parts = (nome_completo or "").strip().split()
    return parts[0] if parts else ""


def _nome_entregador_saida(db: Session, saida: Saida) -> str:
    """Nome completo do entregador/motoboy da saída (preferência: campo entregador)."""
    nome = (saida.entregador or "").strip()
    if nome:
        return nome
    mid = getattr(saida, "motoboy_id", None)
    if not mid:
        return ""
    motoboy = db.get(Motoboy, int(mid))
    if not motoboy or not motoboy.user_id:
        return ""
    user = db.get(User, motoboy.user_id)
    if not user:
        return ""
    return f"{user.nome or ''} {user.sobrenome or ''}".strip() or (user.username or "").strip()


def _build_comprovante_resumo(
    db: Session,
    *,
    saida: Saida,
    detail: Optional[SaidaDetail],
) -> Dict[str, Any]:
    status_label = _status_amigavel(saida.status)
    ocorrencia_dt, assume_utc = _ultima_data_ocorrencia(db, saida)
    data_hora = _format_dt_br(ocorrencia_dt, assume_utc=assume_utc)
    nome_recebedor = (getattr(detail, "nome_recebedor", None) or "").strip() if detail else ""
    motivo = (getattr(detail, "motivo_ocorrencia", None) or "").strip() if detail else ""
    observacao = ""
    if detail:
        if status_label == "Ausente":
            observacao = (detail.observacao_ocorrencia or "").strip()
        else:
            observacao = (detail.observacao_entrega or "").strip()
    entregador_primeiro = _primeiro_nome(_nome_entregador_saida(db, saida))
    label_entregador = "Entregue por" if status_label == "Entregue" else "Motoboy"

    linhas: List[Tuple[str, str]] = [("Código", (saida.codigo or "").strip() or "—")]
    linhas.append(("Status", status_label))
    if data_hora:
        label_dt = "Data/hora da entrega" if status_label == "Entregue" else "Data/hora"
        linhas.append((label_dt, data_hora))
    if nome_recebedor:
        linhas.append(("Recebido por", nome_recebedor))
    if entregador_primeiro:
        linhas.append((label_entregador, entregador_primeiro))
    if motivo and status_label == "Ausente":
        linhas.append(("Motivo", motivo))
    if observacao:
        linhas.append(("Observação", observacao))

    caption_parts = [
        f"Comprovante — {status_label}",
        f"Código: {(saida.codigo or '').strip() or '—'}",
    ]
    if data_hora:
        caption_parts.append(f"{'Entrega' if status_label == 'Entregue' else 'Registro'}: {data_hora}")
    if nome_recebedor:
        caption_parts.append(f"Recebido por: {nome_recebedor}")
    if entregador_primeiro:
        caption_parts.append(f"{label_entregador}: {entregador_primeiro}")
    if motivo and status_label == "Ausente":
        caption_parts.append(f"Motivo: {motivo}")

    return {
        "codigo": (saida.codigo or "").strip(),
        "status": status_label,
        "data_hora": data_hora,
        "nome_recebedor": nome_recebedor or None,
        "entregador": entregador_primeiro or None,
        "motivo": motivo or None,
        "observacao": observacao or None,
        "linhas": linhas,
        "caption": "\n".join(caption_parts),
    }


def _load_font(size: int, bold: bool = False, mono: bool = False):
    from PIL import ImageFont

    candidates = []
    if mono and bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            ]
        )
    elif mono:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
            ]
        )
    elif bold:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ]
        )
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int) -> List[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _load_comprovante_logo(max_width: int = 220, max_height: int = 56):
    """Carrega o logo do sistema para o cartão; retorna None se indisponível."""
    from PIL import Image

    logo_env = (os.getenv("COMPROVANTE_LOGO_PATH") or "").strip()
    candidates = []
    if logo_env:
        candidates.append(Path(logo_env))
    candidates.append(_DEFAULT_LOGO_PATH)

    for path in candidates:
        try:
            if not path.is_file():
                continue
            with Image.open(path) as raw:
                logo = raw.convert("RGBA")
            bbox = logo.getbbox()
            if bbox:
                logo = logo.crop(bbox)
            if logo.width <= 0 or logo.height <= 0:
                continue
            ratio = min(max_width / float(logo.width), max_height / float(logo.height), 1.0)
            if ratio < 1.0:
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                logo = logo.resize(
                    (max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio))),
                    resample,
                )
            return logo
        except Exception:
            logger.warning("comprovante_logo_load_failed path=%s", str(path), exc_info=True)
    return None


def _header_latin1_safe(value: Any, *, max_len: int = 200) -> str:
    """HTTP headers só aceitam latin-1; evita 500 com em dash/unicode (ex.: —portaria)."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""
    for src, dst in (
        ("\u2014", "-"),  # —
        ("\u2013", "-"),  # –
        ("\u2212", "-"),  # −
        ("\u00a0", " "),
    ):
        text = text.replace(src, dst)
    text = text.encode("latin-1", errors="ignore").decode("latin-1").strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def _px(scale: float, value: float) -> int:
    return max(1, int(round(value * scale)))


def _status_seal_colors(status: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]:
    """Texto, ponto e fundo do selo. A cor fica só no sinal de status."""
    if status == "Entregue":
        return (21, 128, 61), (22, 163, 74), (243, 245, 244)
    if _is_status_ocorrencia_label(status):
        return (185, 28, 28), (220, 38, 38), (250, 246, 246)
    return (29, 78, 216), (37, 99, 235), (243, 246, 252)


def _cover_crop_4_3(photo: Any, target_width: int) -> Any:
    from PIL import Image

    target_h = max(1, int(round(target_width * 3 / 4)))
    scale = max(target_width / float(photo.width), target_h / float(photo.height))
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    resized = photo.resize(
        (
            max(1, int(round(photo.width * scale))),
            max(1, int(round(photo.height * scale))),
        ),
        resample,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_width, top + target_h))


def _text_chip(text: str, font: Any, pad_x: int, pad_y: int) -> Any:
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    chip = Image.new("RGBA", (tw + pad_x * 2, th + pad_y * 2), (0, 0, 0, 158))
    ImageDraw.Draw(chip).text(
        (pad_x - bbox[0], pad_y - bbox[1]),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )
    return chip


def _build_comprovante_share_image_bytes(
    *, photo_bytes_list: List[bytes], resumo: Dict[str, Any]
) -> bytes:
    """Monta o cartão do comprovante: selo, código em mono e fotos 4:3.

    O primeiro quadrado (largura × largura) precisa mostrar selo, código, data
    e o início da primeira foto, que é o recorte do preview no WhatsApp.
    """
    from PIL import Image, ImageDraw

    if not photo_bytes_list:
        raise ValueError("Nenhuma foto para montar o comprovante.")

    opened: List[Any] = []
    target_width = 720
    for raw in photo_bytes_list[:3]:
        with Image.open(BytesIO(raw)) as photo_raw:
            photo = photo_raw.convert("RGB")
        target_width = max(target_width, min(1080, photo.width))
        opened.append(photo)
    photos = [_cover_crop_4_3(photo, target_width) for photo in opened]

    scale = target_width / 360.0
    pad = _px(scale, 16)
    ink = (20, 18, 16)
    meta_color = (92, 101, 109)
    rule = (230, 228, 225)
    status = str(resumo.get("status") or "Comprovante")
    codigo = str(resumo.get("codigo") or "").strip() or "—"
    data_hora = str(resumo.get("data_hora") or "").strip()
    entregador = str(resumo.get("entregador") or "").strip()
    meta_parts = [part for part in (data_hora, entregador) if part]
    meta = " · ".join(meta_parts)

    extras: List[Tuple[str, str]] = []
    recebedor = str(resumo.get("nome_recebedor") or "").strip()
    if recebedor:
        extras.append(("Recebido por", recebedor))
    motivo = str(resumo.get("motivo") or "").strip()
    if motivo and _is_status_ocorrencia_label(status):
        extras.append(("Motivo", motivo))
    observacao = str(resumo.get("observacao") or "").strip()
    if observacao:
        extras.append(("Observação", observacao))

    seal_font = _load_font(_px(scale, 12), bold=True)
    code_font = _load_font(_px(scale, 26), mono=True)
    meta_font = _load_font(_px(scale, 13))
    extra_label_font = _load_font(_px(scale, 11), bold=True)
    extra_value_font = _load_font(_px(scale, 13))
    stamp_font = _load_font(_px(scale, 11), mono=True)
    footer_font = _load_font(_px(scale, 12), bold=True)

    probe = Image.new("RGB", (target_width, 10), (255, 255, 255))
    probe_draw = ImageDraw.Draw(probe)
    seal_text_box = probe_draw.textbbox((0, 0), status, font=seal_font)
    seal_tw = seal_text_box[2] - seal_text_box[0]
    seal_th = seal_text_box[3] - seal_text_box[1]
    dot_d = _px(scale, 7)
    seal_pad_x = _px(scale, 8)
    seal_pad_y = _px(scale, 4)
    seal_gap = _px(scale, 6)
    seal_h = max(seal_th, dot_d) + seal_pad_y * 2
    seal_w = seal_pad_x + dot_d + seal_gap + seal_tw + seal_pad_x

    logo = _load_comprovante_logo(
        max_width=max(1, target_width - pad * 2 - seal_w - _px(scale, 12)),
        max_height=_px(scale, 32),
    )
    row_h = max(logo.height if logo is not None else 0, seal_h)
    content_width = target_width - pad * 2

    y = pad + row_h + _px(scale, 14)
    code_box = probe_draw.textbbox((0, 0), codigo, font=code_font)
    y += code_box[3] - code_box[1]
    if meta:
        y += _px(scale, 6)
        meta_box = probe_draw.textbbox((0, 0), meta, font=meta_font)
        y += meta_box[3] - meta_box[1]
    extra_lines: List[Tuple[str, List[str]]] = []
    for label, value in extras:
        y += _px(scale, 10)
        label_box = probe_draw.textbbox((0, 0), label, font=extra_label_font)
        y += label_box[3] - label_box[1] + _px(scale, 2)
        wrapped = _wrap_text(probe_draw, value, extra_value_font, content_width)
        extra_lines.append((label, wrapped))
        for line in wrapped:
            line_box = probe_draw.textbbox((0, 0), line, font=extra_value_font)
            y += line_box[3] - line_box[1] + _px(scale, 2)
    y += _px(scale, 14)
    header_h = y + 1

    footer = "ROTEVO"
    footer_box = probe_draw.textbbox((0, 0), footer, font=footer_font)
    footer_h = _px(scale, 10) + (footer_box[3] - footer_box[1]) + _px(scale, 12)
    photos_h = sum(photo.height for photo in photos)
    canvas = Image.new("RGB", (target_width, header_h + photos_h + footer_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    y = pad
    if logo is not None:
        canvas.paste(logo, (pad, y + (row_h - logo.height) // 2), logo)
    seal_x = target_width - pad - seal_w
    seal_y = y + (row_h - seal_h) // 2
    text_color, dot_color, seal_bg = _status_seal_colors(status)
    seal_box = (seal_x, seal_y, seal_x + seal_w, seal_y + seal_h)
    if hasattr(draw, "rounded_rectangle"):
        draw.rounded_rectangle(seal_box, radius=seal_h // 2, fill=seal_bg)
    else:
        draw.rectangle(seal_box, fill=seal_bg)
    dot_y = seal_y + (seal_h - dot_d) // 2
    draw.ellipse(
        (seal_x + seal_pad_x, dot_y, seal_x + seal_pad_x + dot_d, dot_y + dot_d),
        fill=dot_color,
    )
    draw.text(
        (
            seal_x + seal_pad_x + dot_d + seal_gap - seal_text_box[0],
            seal_y + (seal_h - seal_th) // 2 - seal_text_box[1],
        ),
        status,
        font=seal_font,
        fill=text_color,
    )

    y = pad + row_h + _px(scale, 14)
    draw.text((pad - code_box[0], y - code_box[1]), codigo, font=code_font, fill=ink)
    y += code_box[3] - code_box[1]
    if meta:
        y += _px(scale, 6)
        meta_box = draw.textbbox((0, 0), meta, font=meta_font)
        draw.text((pad - meta_box[0], y - meta_box[1]), meta, font=meta_font, fill=meta_color)
        y += meta_box[3] - meta_box[1]
    for label, wrapped in extra_lines:
        y += _px(scale, 10)
        label_box = draw.textbbox((0, 0), label, font=extra_label_font)
        draw.text((pad - label_box[0], y - label_box[1]), label, font=extra_label_font, fill=meta_color)
        y += label_box[3] - label_box[1] + _px(scale, 2)
        for line in wrapped:
            line_box = draw.textbbox((0, 0), line, font=extra_value_font)
            draw.text((pad - line_box[0], y - line_box[1]), line, font=extra_value_font, fill=ink)
            y += line_box[3] - line_box[1] + _px(scale, 2)
    y += _px(scale, 14)
    draw.line([(0, y), (target_width, y)], fill=rule)

    photo_y = header_h
    stamp_margin = _px(scale, 8)
    chip_pad_x = _px(scale, 6)
    chip_pad_y = _px(scale, 3)
    total = len(photos)
    for index, photo in enumerate(photos):
        canvas.paste(photo, (0, photo_y))
        if index:
            draw.line([(0, photo_y), (target_width, photo_y)], fill=rule)
        code_chip = _text_chip(f"cód. {codigo}", stamp_font, chip_pad_x, chip_pad_y)
        index_chip = _text_chip(f"{index + 1}/{total}", stamp_font, chip_pad_x, chip_pad_y)
        chip_y = photo_y + photo.height - stamp_margin - code_chip.height
        canvas.paste(code_chip, (stamp_margin, chip_y), code_chip)
        canvas.paste(
            index_chip,
            (target_width - stamp_margin - index_chip.width, chip_y),
            index_chip,
        )
        photo_y += photo.height

    footer_box = draw.textbbox((0, 0), footer, font=footer_font)
    draw.text(
        (pad - footer_box[0], photo_y + _px(scale, 10) - footer_box[1]),
        footer,
        font=footer_font,
        fill=(138, 145, 143),
    )

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=85, optimize=True)
    buf.seek(0)
    return buf.getvalue()


def _load_comprovante_image_bytes(
    db: Session, *, saida: Saida, index: int
) -> Tuple[bytes, int, Optional[SaidaDetail]]:
    detail = _load_detail_for_saida(db, saida.id_saida)
    keys = extract_foto_keys(detail.foto_url if detail else None)
    if not keys:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")
    if index >= len(keys):
        raise HTTPException(status_code=404, detail="Índice de comprovante inválido.")

    object_key = extract_object_key(keys[index], B2_BUCKET_NAME)
    client = _get_s3_client()
    try:
        obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=object_key)
        image_bytes = obj["Body"].read()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erro ao baixar comprovante: {e}")
    return image_bytes, len(keys), detail


def _select_keys_mesmo_evento(detail: Optional[SaidaDetail], index: int) -> List[str]:
    """Seleciona até 3 fotos do mesmo evento/tentativa da foto escolhida (pacote + endereço etc.)."""
    items = parse_foto_items(detail.foto_url if detail else None)
    if not items:
        return []
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Índice de comprovante inválido.")
    pivot = items[index]
    evento = str(pivot.get("evento") or "legacy").lower()
    tentativa = int(pivot.get("tentativa") or 1)
    selected = [
        str(item["key"])
        for item in items
        if item.get("key")
        and str(item.get("evento") or "legacy").lower() == evento
        and int(item.get("tentativa") or 1) == tentativa
    ]
    # Legado sem tipagem: inclui todas as keys (até 3).
    if evento == "legacy" and len(selected) <= 1:
        selected = [str(item["key"]) for item in items if item.get("key")]
    return selected[:3]


def _download_comprovante_keys(keys: List[str]) -> List[bytes]:
    client = _get_s3_client()
    out: List[bytes] = []
    for key in keys:
        object_key = extract_object_key(key, B2_BUCKET_NAME)
        try:
            obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=object_key)
            out.append(obj["Body"].read())
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Erro ao baixar comprovante: {e}")
    if not out:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")
    return out


# ---------- POST /upload/presign ----------


@router.post("/presign")
def upload_presign(
    body: PresignIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retorna URL presigned PUT e object_key para o cliente enviar o binário direto ao B2."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    tipo = (body.tipo or "").strip().lower()
    if body.id_saida is None:
        if tipo != "lancar_avulso":
            raise HTTPException(status_code=422, detail="id_saida é obrigatório para este tipo.")
    else:
        saida = _ensure_saida_owned(db, sub_base, body.id_saida)
        _ensure_motoboy_owns_saida(current_user, saida)

    ext = "jpg"
    if body.filename and "." in body.filename:
        ext = body.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
    if body.id_saida is None:
        object_key = f"saida/pending/{tipo}/{uuid.uuid4().hex}.{ext}"
    else:
        object_key = f"saida/{body.id_saida}/{tipo}/{uuid.uuid4().hex}.{ext}"

    client = _get_s3_client()
    params = {"Bucket": B2_BUCKET_NAME, "Key": object_key}
    if body.content_type:
        params["ContentType"] = body.content_type.strip()

    try:
        upload_url = client.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=300,
        )
    except Exception as e:
        logger.exception("upload_presign_failed tipo=%s id_saida=%s", body.tipo, body.id_saida)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "STORAGE_TEMPORARY_ERROR",
                "message": "Falha temporária ao preparar o envio da foto. Tente novamente.",
            },
        ) from e

    logger.info(
        "upload presign: id_saida=%s tipo=%s photo_id=%s object_key=%s user_id=%s",
        body.id_saida,
        body.tipo,
        (body.photo_id or "").strip() or None,
        object_key,
        getattr(current_user, "id", None),
    )
    return {
        "upload_url": upload_url,
        "object_key": object_key,
        "headers": {"Content-Type": body.content_type or "image/jpeg"},
    }


# ---------- POST /upload/avulso-file ----------
# Proxy para o web: evita PUT direto no B2 (CORS → "Failed to fetch" no browser).

_MAX_AVULSO_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB
_ALLOWED_AVULSO_CONTENT_TYPES = frozenset({
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/webp",
})


@router.post("/avulso-file")
async def upload_avulso_file(
    file: UploadFile = File(...),
    photo_id: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recebe a imagem do avulso e grava no B2 (pending). Uso preferencial do web."""
    del db  # auth/tenant já validados pelo current_user
    sub_base = getattr(current_user, "sub_base", None)
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    filename = (file.filename or "avulso.jpg").strip() or "avulso.jpg"
    content_type = (file.content_type or "").strip().lower() or "image/jpeg"
    if content_type not in _ALLOWED_AVULSO_CONTENT_TYPES:
        # Alguns browsers mandam application/octet-stream; aceita e trata como jpeg.
        if content_type not in ("application/octet-stream", ""):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "FOTO_TIPO_INVALIDO",
                    "message": "Envie uma imagem JPG, PNG, GIF ou WEBP.",
                },
            )
        content_type = "image/jpeg"

    raw = await file.read()
    if not raw:
        raise HTTPException(
            status_code=422,
            detail={"code": "FOTO_VAZIA", "message": "Arquivo de imagem vazio."},
        )
    if len(raw) > _MAX_AVULSO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "FOTO_MUITO_GRANDE",
                "message": "Imagem muito grande. Use um arquivo de até 8 MB.",
            },
        )

    ext = "jpg"
    if "." in filename:
        cand = filename.rsplit(".", 1)[-1].lower()
        if cand in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = cand
    object_key = f"saida/pending/lancar_avulso/{uuid.uuid4().hex}.{ext}"
    pid = (photo_id or "").strip() or None

    client = _get_s3_client()
    try:
        client.put_object(
            Bucket=B2_BUCKET_NAME,
            Key=object_key,
            Body=raw,
            ContentType=content_type,
        )
    except Exception as e:
        logger.exception(
            "upload_avulso_file_failed user_id=%s key=%s",
            getattr(current_user, "id", None),
            object_key,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "STORAGE_TEMPORARY_ERROR",
                "message": "Falha temporária ao enviar a foto. Tente novamente.",
            },
        ) from e

    logger.info(
        "upload avulso-file: photo_id=%s object_key=%s user_id=%s bytes=%s",
        pid,
        object_key,
        getattr(current_user, "id", None),
        len(raw),
    )
    return {
        "object_key": object_key,
        "foto_object_key": object_key,
        "photo_id": pid,
    }


# ---------- POST /upload/presign-get ----------


@router.post("/presign-get")
def upload_presign_get(
    body: PresignGetIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retorna URL(s) presigned GET para exibir imagem(ns) do bucket privado."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    keys: List[str] = []
    if body.foto_urls:
        for u in body.foto_urls:
            if u and str(u).strip():
                try:
                    keys.append(extract_object_key(str(u).strip(), B2_BUCKET_NAME))
                except ValueError:
                    pass
    elif body.foto_url and str(body.foto_url).strip():
        try:
            keys.append(extract_object_key(str(body.foto_url).strip(), B2_BUCKET_NAME))
        except ValueError:
            raise HTTPException(status_code=422, detail="foto_url inválida.")

    if not keys:
        raise HTTPException(status_code=422, detail="Informe foto_url ou foto_urls.")

    id_saida_hint = int(body.id_saida) if body.id_saida else None
    for key in keys:
        _ensure_object_key_owned(db, sub_base, key, id_saida_hint=id_saida_hint)

    client = _get_s3_client()
    expires_in = 60
    urls = []
    for key in keys:
        try:
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": B2_BUCKET_NAME, "Key": key},
                ExpiresIn=expires_in,
            )
            urls.append(url)
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Erro ao gerar URL de leitura: {e}",
            )

    if len(urls) == 1 and not body.foto_urls:
        return {"download_url": urls[0], "expires_in": expires_in}
    return {"download_urls": urls, "expires_in": expires_in}


@router.get("/saida/{id_saida}/comprovante-watermark", response_model=WatermarkFotoOut)
def get_comprovante_watermark(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")
    saida = _ensure_saida_owned(db, sub_base, id_saida)
    _ensure_motoboy_owns_saida(current_user, saida)

    detail = db.execute(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    ).scalar_one_or_none()

    keys = extract_foto_keys(detail.foto_url if detail else None)
    if not keys:
        return {"tem_comprovante": False, "image_count": 0, "image_url": None}

    return {
        "tem_comprovante": True,
        "image_count": len(keys),
        "image_url": f"/api/upload/saida/{id_saida}/comprovante-watermark/image",
    }


@router.get("/saida/{id_saida}/comprovante-watermark/image")
def get_comprovante_watermark_image(
    id_saida: int,
    index: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    saida = _ensure_saida_owned(db, sub_base, id_saida)
    _ensure_motoboy_owns_saida(current_user, saida)
    image_bytes, _, _ = _load_comprovante_image_bytes(db, saida=saida, index=index)

    try:
        watermarked = _build_watermark_image_bytes(image_bytes=image_bytes, codigo=saida.codigo or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar watermark: {e}")

    return StreamingResponse(
        BytesIO(watermarked),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=30"},
    )


@router.post("/saida/{id_saida}/comprovante-export")
def export_comprovante(
    id_saida: int,
    body: ComprovanteExportIn = ComprovanteExportIn(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Exporta JPEG do comprovante: selo, código e até 3 fotos 4:3 do mesmo evento."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")
    saida = _ensure_saida_owned(db, sub_base, id_saida)
    _ensure_motoboy_owns_saida(current_user, saida)

    detail = _load_detail_for_saida(db, saida.id_saida)
    keys = _select_keys_mesmo_evento(detail, body.index)
    if not keys:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")
    image_list = _download_comprovante_keys(keys)
    try:
        resumo = _build_comprovante_resumo(db, saida=saida, detail=detail)
        share_image = _build_comprovante_share_image_bytes(
            photo_bytes_list=image_list, resumo=resumo
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar comprovante: {e}")

    logger.info(
        "export_share: id_saida=%s index=%s fotos=%s status=%s has_recebedor=%s user_id=%s",
        id_saida,
        body.index,
        len(image_list),
        resumo.get("status"),
        bool(resumo.get("nome_recebedor")),
        getattr(current_user, "id", None),
    )

    raw_filename = f"comprovante-{(saida.codigo or id_saida)}.jpg".replace("/", "-")
    filename = _header_latin1_safe(raw_filename, max_len=180) or "comprovante.jpg"
    # Metadados em header precisam ser latin-1; o JPEG já traz o texto completo (Unicode).
    return StreamingResponse(
        BytesIO(share_image),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Comprovante-Codigo": _header_latin1_safe(resumo.get("codigo")),
            "X-Comprovante-Status": _header_latin1_safe(resumo.get("status")),
            "X-Comprovante-Data": _header_latin1_safe(resumo.get("data_hora")),
            "X-Comprovante-Recebedor": _header_latin1_safe(resumo.get("nome_recebedor")),
            "X-Comprovante-Entregador": _header_latin1_safe(resumo.get("entregador")),
            "X-Comprovante-Index": str(body.index),
            "X-Comprovante-Fotos": str(len(image_list)),
        },
    )
