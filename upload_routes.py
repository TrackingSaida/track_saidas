"""
Rotas de upload: presigned PUT (B2) para o mobile enviar foto direto;
presigned GET para o web exibir imagens do bucket privado.
Prefixo: /upload. Auth: get_current_user (web e mobile).
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from io import BytesIO
from typing import Optional, List

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from botocore.client import Config
import boto3

from sqlalchemy.orm import Session
from sqlalchemy import select

from auth import get_current_user
from db import get_db
from models import User, Saida, SaidaDetail

router = APIRouter(prefix="/upload", tags=["Upload - Fotos entrega"])

# Env: B2_BUCKET_NAME, B2_ACCESS_KEY_ID, B2_SECRET_ACCESS_KEY, B2_ENDPOINT_URL
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "ts-prod-entregas-fotos")
B2_ACCESS_KEY_ID = os.getenv("B2_ACCESS_KEY_ID", "")
B2_SECRET_ACCESS_KEY = os.getenv("B2_SECRET_ACCESS_KEY", "")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "https://s3.us-east-005.backblazeb2.com")

# Region extraída do endpoint (ex: s3.us-east-005.backblazeb2.com -> us-east-005)
_B2_REGION = "us-east-005"
if "backblazeb2.com" in B2_ENDPOINT_URL:
    m = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", B2_ENDPOINT_URL)
    if m:
        _B2_REGION = m.group(1)


def _get_s3_client():
    if not B2_ACCESS_KEY_ID or not B2_SECRET_ACCESS_KEY:
        raise HTTPException(
            status_code=503,
            detail="Upload não configurado (B2 credentials ausentes).",
        )
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL.rstrip("/"),
        aws_access_key_id=B2_ACCESS_KEY_ID,
        aws_secret_access_key=B2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name=_B2_REGION,
    )


def _ensure_saida_owned(db, sub_base: str, id_saida: int) -> None:
    s = db.get(Saida, id_saida)
    if not s or s.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Saída não encontrada.")


# ---------- Schemas ----------


class PresignIn(BaseModel):
    filename: str = Field(min_length=1)
    id_saida: int = Field(gt=0)
    tipo: str = Field(pattern="^(entregue|ausente)$")
    content_type: str = Field(default="image/jpeg")


class PresignGetIn(BaseModel):
    foto_url: Optional[str] = None
    foto_urls: Optional[List[str]] = None


class WatermarkFotoOut(BaseModel):
    tem_comprovante: bool
    image_url: Optional[str] = None


def _extract_object_key(foto_url: str, bucket_name: str) -> str:
    """De foto_url (object_key ou URL completa) retorna o object_key."""
    s = (foto_url or "").strip()
    if not s:
        raise ValueError("foto_url vazia")
    if s.startswith("http://") or s.startswith("https://"):
        # Extrair key após /bucket_name/
        prefix = f"/{bucket_name}/"
        idx = s.find(prefix)
        if idx != -1:
            return s[idx + len(prefix) :].split("?")[0]
        # Fallback: path após o último /
        return s.split("/")[-1].split("?")[0] or s
    return s


def _extract_foto_keys(detail_foto_url: Optional[str]) -> List[str]:
    raw = (detail_foto_url or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            return [raw]
    return [raw]


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
    _ensure_saida_owned(db, sub_base, body.id_saida)

    ext = "jpg"
    if body.filename and "." in body.filename:
        ext = body.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"
    # object_key: saida/{id_saida}/{tipo}/{uuid}.ext (prefixo saida/ obrigatório na Application Key)
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
        "upload presign: id_saida=%s tipo=%s object_key=%s user_id=%s",
        body.id_saida,
        body.tipo,
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
    current_user: User = Depends(get_current_user),
):
    """Retorna URL(s) presigned GET para exibir imagem(ns) do bucket privado."""
    if not current_user.sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    keys: List[str] = []
    if body.foto_urls:
        for u in body.foto_urls:
            if u and str(u).strip():
                try:
                    keys.append(_extract_object_key(str(u).strip(), B2_BUCKET_NAME))
                except ValueError:
                    pass
    elif body.foto_url and str(body.foto_url).strip():
        try:
            keys.append(_extract_object_key(str(body.foto_url).strip(), B2_BUCKET_NAME))
        except ValueError:
            raise HTTPException(status_code=422, detail="foto_url inválida.")

    if not keys:
        raise HTTPException(status_code=422, detail="Informe foto_url ou foto_urls.")

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
    _ensure_saida_owned(db, sub_base, id_saida)

    detail = db.execute(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    ).scalar_one_or_none()

    keys = _extract_foto_keys(detail.foto_url if detail else None)
    if not keys:
        return {"tem_comprovante": False, "image_url": None}

    return {
        "tem_comprovante": True,
        "image_url": f"/api/upload/saida/{id_saida}/comprovante-watermark/image",
    }


@router.get("/saida/{id_saida}/comprovante-watermark/image")
def get_comprovante_watermark_image(
    id_saida: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = current_user.sub_base
    if not sub_base:
        raise HTTPException(status_code=401, detail="Usuário inválido.")

    saida = db.get(Saida, id_saida)
    if not saida or saida.sub_base != sub_base:
        raise HTTPException(status_code=404, detail="Saída não encontrada.")

    detail = db.execute(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    ).scalar_one_or_none()
    keys = _extract_foto_keys(detail.foto_url if detail else None)
    if not keys:
        raise HTTPException(status_code=404, detail="Comprovante não encontrado.")

    object_key = _extract_object_key(keys[-1], B2_BUCKET_NAME)
    client = _get_s3_client()
    try:
        obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=object_key)
        image_bytes = obj["Body"].read()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Erro ao baixar comprovante: {e}")

    try:
        watermarked = _build_watermark_image_bytes(image_bytes=image_bytes, codigo=saida.codigo or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar watermark: {e}")

    return StreamingResponse(
        BytesIO(watermarked),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=30"},
    )
