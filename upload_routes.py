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

from fastapi import APIRouter, Depends, HTTPException, Query
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
        raise HTTPException(status_code=404, detail="Saída não encontrada.")


def _ensure_object_key_owned(db: Session, sub_base: str, object_key: str) -> None:
    id_saida = parse_id_saida_from_object_key(object_key)
    if id_saida is None:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")
    _ensure_saida_owned(db, sub_base, id_saida)


# ---------- Schemas ----------


class PresignIn(BaseModel):
    filename: str = Field(min_length=1)
    id_saida: int = Field(gt=0)
    tipo: str = Field(pattern="^(entregue|ausente|devolucao)$")
    content_type: str = Field(default="image/jpeg")
    photo_id: Optional[str] = Field(default=None, max_length=80)


class PresignGetIn(BaseModel):
    foto_url: Optional[str] = None
    foto_urls: Optional[List[str]] = None


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


def _format_dt_br(value: Optional[datetime]) -> str:
    if not value:
        return ""
    try:
        dt = value
        if dt.tzinfo is None:
            # Timestamps operacionais são gravados como UTC naive na maioria dos fluxos.
            dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(OPERACAO_TZ)
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


def _ultima_data_ocorrencia(db: Session, saida: Saida) -> Optional[datetime]:
    if saida.data_hora_entrega:
        return saida.data_hora_entrega
    status_norm = (saida.status or "").strip().upper().replace(" ", "_")
    eventos = ("entregue", "entregue_lote") if status_norm == "ENTREGUE" else ("ausente", "ausente_lote", "entregue", "ausente")
    return db.execute(
        select(func.max(SaidaHistorico.timestamp)).where(
            SaidaHistorico.id_saida == saida.id_saida,
            SaidaHistorico.evento.in_(eventos),
        )
    ).scalar_one_or_none()


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
    data_hora = _format_dt_br(_ultima_data_ocorrencia(db, saida))
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


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = []
    if bold:
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


def _build_comprovante_share_image_bytes(
    *, photo_bytes_list: List[bytes], resumo: Dict[str, Any]
) -> bytes:
    """Monta cartão com logo + dados + até 3 fotos empilhadas (WhatsApp-friendly)."""
    from PIL import Image, ImageDraw

    if not photo_bytes_list:
        raise ValueError("Nenhuma foto para montar o comprovante.")

    photos: List[Any] = []
    target_width = 720
    for raw in photo_bytes_list[:3]:
        with Image.open(BytesIO(raw)) as photo_raw:
            photo = photo_raw.convert("RGB")
        target_width = max(target_width, min(1080, photo.width))
        photos.append(photo)

    resized: List[Any] = []
    for photo in photos:
        if photo.width != target_width:
            ratio = target_width / float(photo.width)
            photo = photo.resize((target_width, max(1, int(photo.height * ratio))))
        resized.append(photo)
    photos = resized

    pad_x = 28
    pad_y = 24
    gap = 10
    title_font = _load_font(34, bold=True)
    label_font = _load_font(22, bold=True)
    value_font = _load_font(24, bold=False)
    footer_font = _load_font(18, bold=False)

    status = str(resumo.get("status") or "Comprovante")
    title = f"Comprovante — {status}"
    linhas: List[Tuple[str, str]] = list(resumo.get("linhas") or [])
    logo = _load_comprovante_logo()

    # Mede altura do cabeçalho
    probe = Image.new("RGB", (target_width, 10), (255, 255, 255))
    probe_draw = ImageDraw.Draw(probe)
    content_width = target_width - (pad_x * 2)
    y = pad_y
    if logo is not None:
        y += logo.height + 14
    y += probe_draw.textbbox((0, 0), title, font=title_font)[3] + 16
    for label, value in linhas:
        y += probe_draw.textbbox((0, 0), label, font=label_font)[3] + 4
        for wrapped in _wrap_text(probe_draw, str(value), value_font, content_width):
            y += probe_draw.textbbox((0, 0), wrapped, font=value_font)[3] + 2
        y += 10
    y += 8
    header_h = y + pad_y

    photos_h = sum(p.height for p in photos) + gap * max(0, len(photos) - 1)
    canvas = Image.new("RGB", (target_width, header_h + photos_h), (248, 250, 252))
    draw = ImageDraw.Draw(canvas)

    # Faixa de status
    accent = (22, 163, 74) if status == "Entregue" else ((220, 38, 38) if status == "Ausente" else (37, 99, 235))
    draw.rectangle([(0, 0), (target_width, 8)], fill=accent)

    y = pad_y + 4
    if logo is not None:
        canvas.paste(logo, (pad_x, y), logo)
        y += logo.height + 14

    draw.text((pad_x, y), title, font=title_font, fill=(15, 23, 42))
    y += draw.textbbox((0, 0), title, font=title_font)[3] + 16

    for label, value in linhas:
        draw.text((pad_x, y), label, font=label_font, fill=(100, 116, 139))
        y += draw.textbbox((0, 0), label, font=label_font)[3] + 4
        for wrapped in _wrap_text(draw, str(value), value_font, content_width):
            draw.text((pad_x, y), wrapped, font=value_font, fill=(15, 23, 42))
            y += draw.textbbox((0, 0), wrapped, font=value_font)[3] + 2
        y += 10

    footer = "TrackingSaída — comprovante operacional"
    draw.text((pad_x, header_h - pad_y - 4), footer, font=footer_font, fill=(148, 163, 184))

    y = header_h
    for i, photo in enumerate(photos):
        canvas.paste(photo, (0, y))
        y += photo.height
        if i < len(photos) - 1:
            y += gap

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
    saida = _ensure_saida_owned(db, sub_base, body.id_saida)
    _ensure_motoboy_owns_saida(current_user, saida)

    ext = "jpg"
    if body.filename and "." in body.filename:
        ext = body.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
    object_key = f"saida/{body.id_saida}/{body.tipo}/{uuid.uuid4().hex}.{ext}"

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
        raise HTTPException(status_code=503, detail=f"Erro ao gerar presigned URL: {e}")

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

    for key in keys:
        _ensure_object_key_owned(db, sub_base, key)

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
    """Exporta JPEG com cartão + até 3 fotos do mesmo evento (empilhadas, WhatsApp-friendly)."""
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
        watermarked_list = [
            _build_watermark_image_bytes(image_bytes=img, codigo=saida.codigo or "")
            for img in image_list
        ]
        resumo = _build_comprovante_resumo(db, saida=saida, detail=detail)
        share_image = _build_comprovante_share_image_bytes(
            photo_bytes_list=watermarked_list, resumo=resumo
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar comprovante: {e}")

    logger.info(
        "export_share: id_saida=%s index=%s fotos=%s status=%s has_recebedor=%s user_id=%s",
        id_saida,
        body.index,
        len(watermarked_list),
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
            "X-Comprovante-Fotos": str(len(watermarked_list)),
        },
    )
