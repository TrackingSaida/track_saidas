"""
Rotas de upload: presigned PUT (B2) para o mobile enviar foto direto;
presigned GET para o web exibir imagens do bucket privado.
Prefixo: /upload. Auth: get_current_user (web e mobile).
"""
from __future__ import annotations

import logging
import uuid
from io import BytesIO
from typing import Optional, List

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session
from sqlalchemy import select

from auth import get_current_user
from db import get_db
from models import User, Saida, SaidaDetail
from upload_storage_utils import (
    B2_BUCKET_NAME,
    extract_foto_keys,
    extract_object_key,
    get_s3_client_optional,
    parse_id_saida_from_object_key,
)

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
    tipo: str = Field(pattern="^(entregue|ausente)$")
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


def _load_comprovante_image_bytes(db: Session, *, saida: Saida, index: int) -> tuple[bytes, int]:
    detail = db.execute(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == saida.id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    ).scalar_one_or_none()
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
    return image_bytes, len(keys)


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
    image_bytes, _ = _load_comprovante_image_bytes(db, saida=saida, index=index)

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
    """Exporta JPEG watermarkado com metadados mínimos (sem PII)."""
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")
    saida = _ensure_saida_owned(db, sub_base, id_saida)
    _ensure_motoboy_owns_saida(current_user, saida)

    image_bytes, total = _load_comprovante_image_bytes(db, saida=saida, index=body.index)
    try:
        watermarked = _build_watermark_image_bytes(image_bytes=image_bytes, codigo=saida.codigo or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar watermark: {e}")

    status_label = _status_amigavel(saida.status)
    data_hora = ""
    if saida.data_hora_entrega:
        data_hora = saida.data_hora_entrega.isoformat()
    elif saida.timestamp:
        data_hora = saida.timestamp.isoformat()

    logger.info(
        "export_share: id_saida=%s index=%s total=%s status=%s user_id=%s",
        id_saida,
        body.index,
        total,
        status_label,
        getattr(current_user, "id", None),
    )

    filename = f"comprovante-{(saida.codigo or id_saida)}.jpg".replace("/", "-")
    return StreamingResponse(
        BytesIO(watermarked),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Comprovante-Codigo": (saida.codigo or "").strip(),
            "X-Comprovante-Status": status_label,
            "X-Comprovante-Data": data_hora,
            "X-Comprovante-Index": str(body.index),
        },
    )
