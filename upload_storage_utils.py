"""Utilitários compartilhados para storage B2 (upload e purge de limpeza D-60)."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

import boto3
from botocore.client import Config

logger = logging.getLogger(__name__)

B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "ts-prod-entregas-fotos")
B2_ACCESS_KEY_ID = os.getenv("B2_ACCESS_KEY_ID", "")
B2_SECRET_ACCESS_KEY = os.getenv("B2_SECRET_ACCESS_KEY", "")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "https://s3.us-east-005.backblazeb2.com")

_B2_REGION = "us-east-005"
if "backblazeb2.com" in B2_ENDPOINT_URL:
    match = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", B2_ENDPOINT_URL)
    if match:
        _B2_REGION = match.group(1)


def b2_configured() -> bool:
    return bool(B2_ACCESS_KEY_ID and B2_SECRET_ACCESS_KEY)


def get_s3_client_optional():
    if not b2_configured():
        return None
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL.rstrip("/"),
        aws_access_key_id=B2_ACCESS_KEY_ID,
        aws_secret_access_key=B2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name=_B2_REGION,
    )


def extract_object_key(foto_url: str, bucket_name: str = B2_BUCKET_NAME) -> str:
    """De foto_url (object_key ou URL completa) retorna o object_key."""
    value = (foto_url or "").strip()
    if not value:
        raise ValueError("foto_url vazia")
    if value.startswith("http://") or value.startswith("https://"):
        prefix = f"/{bucket_name}/"
        idx = value.find(prefix)
        if idx != -1:
            return value[idx + len(prefix) :].split("?")[0]
        return value.split("/")[-1].split("?")[0] or value
    return value


def extract_foto_keys(detail_foto_url: Optional[str]) -> List[str]:
    raw = (detail_foto_url or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            return [raw]
    return [raw]


def collect_b2_keys_from_foto_urls(foto_urls: List[Optional[str]]) -> List[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for foto_url in foto_urls:
        for raw_key in extract_foto_keys(foto_url):
            try:
                key = extract_object_key(raw_key, B2_BUCKET_NAME)
            except ValueError:
                continue
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def purge_b2_keys(keys: List[str], *, chunk_size: int = 100) -> tuple[int, int]:
    """
    Remove objetos do bucket B2 (best-effort).
    Retorna (deleted, failed).
    """
    if not keys:
        return 0, 0

    client = get_s3_client_optional()
    if client is None:
        logger.warning("b2_purge_skipped: credenciais ausentes")
        return 0, len(keys)

    deleted = 0
    failed = 0
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        objects = [{"Key": key} for key in chunk]
        try:
            response = client.delete_objects(
                Bucket=B2_BUCKET_NAME,
                Delete={"Objects": objects, "Quiet": True},
            )
            deleted += len(response.get("Deleted") or [])
            failed += len(response.get("Errors") or [])
        except Exception:
            logger.exception("b2_purge_chunk_failed")
            failed += len(chunk)
    return deleted, failed
