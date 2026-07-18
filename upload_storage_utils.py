"""Utilitários compartilhados para storage B2 (upload e purge de limpeza D-60)."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

import boto3
from botocore.client import Config

logger = logging.getLogger(__name__)

B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "ts-prod-entregas-fotos")
B2_ACCESS_KEY_ID = os.getenv("B2_ACCESS_KEY_ID", "")
B2_SECRET_ACCESS_KEY = os.getenv("B2_SECRET_ACCESS_KEY", "")
B2_ENDPOINT_URL = os.getenv("B2_ENDPOINT_URL", "https://s3.us-east-005.backblazeb2.com")

MAX_FOTOS_POR_EVENTO_TENTATIVA = 3
EVENTOS_FOTO = frozenset({"entregue", "ausente", "legacy"})

_B2_REGION = "us-east-005"
if "backblazeb2.com" in B2_ENDPOINT_URL:
    match = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", B2_ENDPOINT_URL)
    if match:
        _B2_REGION = match.group(1)

_OBJECT_KEY_SAIDA_RE = re.compile(r"^saida/(\d+)/")


class FotoItem(TypedDict, total=False):
    key: str
    evento: str
    tentativa: int
    photo_id: Optional[str]
    created_at: Optional[str]


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


def parse_id_saida_from_object_key(object_key: str) -> Optional[int]:
    """Extrai id_saida de keys no padrão saida/{id}/..."""
    key = (object_key or "").strip()
    match = _OBJECT_KEY_SAIDA_RE.match(key)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _coerce_foto_item(raw: Any) -> Optional[FotoItem]:
    if isinstance(raw, str):
        key = raw.strip()
        if not key:
            return None
        return FotoItem(key=key, evento="legacy", tentativa=1, photo_id=None, created_at=None)
    if isinstance(raw, dict):
        key = str(raw.get("key") or "").strip()
        if not key:
            return None
        evento_raw = str(raw.get("evento") or "legacy").strip().lower()
        evento = evento_raw if evento_raw in EVENTOS_FOTO else "legacy"
        try:
            tentativa = int(raw.get("tentativa") or 1)
        except (TypeError, ValueError):
            tentativa = 1
        if tentativa < 1:
            tentativa = 1
        photo_id = raw.get("photo_id")
        photo_id_norm = str(photo_id).strip() if photo_id is not None and str(photo_id).strip() else None
        created_at = raw.get("created_at")
        created_at_norm = str(created_at).strip() if created_at is not None and str(created_at).strip() else None
        return FotoItem(
            key=key,
            evento=evento,
            tentativa=tentativa,
            photo_id=photo_id_norm,
            created_at=created_at_norm,
        )
    return None


def parse_foto_items(detail_foto_url: Optional[str]) -> List[FotoItem]:
    """Aceita key única, array de strings ou array de objetos tipados."""
    raw = (detail_foto_url or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items: List[FotoItem] = []
                for entry in parsed:
                    item = _coerce_foto_item(entry)
                    if item:
                        items.append(item)
                return items
        except Exception:
            item = _coerce_foto_item(raw)
            return [item] if item else []
    item = _coerce_foto_item(raw)
    return [item] if item else []


def extract_foto_keys(detail_foto_url: Optional[str]) -> List[str]:
    return [item["key"] for item in parse_foto_items(detail_foto_url) if item.get("key")]


def serialize_foto_items(items: List[FotoItem]) -> str:
    payload: List[Dict[str, Any]] = []
    for item in items:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        entry: Dict[str, Any] = {
            "key": key,
            "evento": str(item.get("evento") or "legacy"),
            "tentativa": int(item.get("tentativa") or 1),
        }
        if item.get("photo_id"):
            entry["photo_id"] = item["photo_id"]
        if item.get("created_at"):
            entry["created_at"] = item["created_at"]
        payload.append(entry)
    return json.dumps(payload, ensure_ascii=False)


def count_fotos_for_evento_tentativa(items: List[FotoItem], evento: str, tentativa: int) -> int:
    evento_norm = (evento or "").strip().lower()
    return sum(
        1
        for item in items
        if str(item.get("evento") or "").lower() == evento_norm
        and int(item.get("tentativa") or 1) == int(tentativa)
    )


def find_foto_item(
    items: List[FotoItem],
    *,
    key: Optional[str] = None,
    photo_id: Optional[str] = None,
) -> Optional[FotoItem]:
    key_norm = (key or "").strip()
    photo_norm = (photo_id or "").strip()
    for item in items:
        if photo_norm and str(item.get("photo_id") or "").strip() == photo_norm:
            return item
        if key_norm and str(item.get("key") or "").strip() == key_norm:
            return item
    return None


def build_foto_item(
    *,
    key: str,
    evento: str,
    tentativa: int,
    photo_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> FotoItem:
    evento_norm = (evento or "").strip().lower()
    if evento_norm not in ("entregue", "ausente"):
        evento_norm = "legacy"
    return FotoItem(
        key=key.strip(),
        evento=evento_norm,
        tentativa=max(1, int(tentativa or 1)),
        photo_id=(photo_id or "").strip() or None,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )


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
