"""
Rotas de upload: presigned PUT (B2) para o mobile enviar foto direto;
presigned GET para o web exibir imagens do bucket privado.
Prefixo: /upload. Auth: get_current_user (web e mobile).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field
from botocore.client import Config
import boto3

from sqlalchemy.orm import Session

from auth import get_current_user
from db import get_db
from models import User, Saida

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
    # object_key: saidas/{id_saida}/{tipo}/{uuid}.ext (prefixo saidas/ obrigatório na Application Key)
    object_key = f"saidas/{body.id_saida}/{body.tipo}/{uuid.uuid4().hex}.{ext}"

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
