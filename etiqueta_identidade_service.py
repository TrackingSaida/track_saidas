"""Identidade visual do Owner para etiquetas (nome + logo com fallback ROTEVO)."""
from __future__ import annotations

import logging
import os
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

from models import Owner
from upload_storage_utils import B2_BUCKET_NAME, get_s3_client_optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_LOGO_PATH = _BASE_DIR / "assets" / "logo-comprovante.png"

LOGO_ORIGEM_OWNER = "owner"
LOGO_ORIGEM_ROTEVO = "rotevo"


def resolver_nome_exibicao(owner: Optional[Owner]) -> str:
    """Prioridade: nome_fantasia → username → sub_base."""
    if owner is None:
        return "ROTEVO"
    nome = (getattr(owner, "nome_fantasia", None) or "").strip()
    if nome:
        return nome
    username = (getattr(owner, "username", None) or "").strip()
    if username:
        return username
    sub_base = (getattr(owner, "sub_base", None) or "").strip()
    return sub_base or "ROTEVO"


def resolver_slogan(owner: Optional[Owner]) -> str:
    if owner is None:
        return ""
    return (getattr(owner, "slogan", None) or "").strip()


def resolver_contato(owner: Optional[Owner]) -> str:
    if owner is None:
        return ""
    return (getattr(owner, "contato", None) or "").strip()


def _load_rotevo_logo_bytes() -> Optional[bytes]:
    logo_env = (os.getenv("COMPROVANTE_LOGO_PATH") or "").strip()
    candidates = []
    if logo_env:
        candidates.append(Path(logo_env))
    candidates.append(_DEFAULT_LOGO_PATH)
    for path in candidates:
        try:
            if path.is_file():
                return path.read_bytes()
        except Exception:
            logger.warning("rotevo_logo_load_failed path=%s", str(path), exc_info=True)
    return None


def _download_owner_logo_bytes(object_key: str) -> Optional[bytes]:
    key = (object_key or "").strip()
    if not key:
        return None
    client = get_s3_client_optional()
    if client is None:
        return None
    try:
        resp = client.get_object(Bucket=B2_BUCKET_NAME, Key=key)
        body = resp["Body"].read()
        return body if body else None
    except Exception:
        logger.warning("owner_logo_download_failed key=%s", key, exc_info=True)
        return None


def _validate_image_bytes(raw: bytes) -> bool:
    if not raw:
        return False
    try:
        with Image.open(BytesIO(raw)) as img:
            img.verify()
        with Image.open(BytesIO(raw)) as img2:
            img2.load()
            if img2.width <= 0 or img2.height <= 0:
                return False
        return True
    except Exception:
        return False


def resolver_logo_etiqueta(owner: Optional[Owner]) -> Tuple[Optional[bytes], str]:
    """
    Retorna (bytes_imagem, origem).
    Nunca propaga erro de logo do Owner — sempre tenta fallback ROTEVO.
    """
    key = (getattr(owner, "logo_object_key", None) or "").strip() if owner else ""
    if key:
        raw = _download_owner_logo_bytes(key)
        if raw and _validate_image_bytes(raw):
            return raw, LOGO_ORIGEM_OWNER

    rotevo = _load_rotevo_logo_bytes()
    if rotevo and _validate_image_bytes(rotevo):
        return rotevo, LOGO_ORIGEM_ROTEVO
    return None, LOGO_ORIGEM_ROTEVO


def fit_logo_image(
    raw: bytes,
    *,
    max_width: int,
    max_height: int,
) -> Optional[Image.Image]:
    """Redimensiona preservando aspect ratio (contain). Nunca distorce."""
    try:
        with Image.open(BytesIO(raw)) as img:
            logo = img.convert("RGBA")
        if logo.width <= 0 or logo.height <= 0:
            return None
        ratio = min(max_width / float(logo.width), max_height / float(logo.height), 1.0)
        if ratio < 1.0:
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
            logo = logo.resize(
                (max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio))),
                resample,
            )
        return logo
    except Exception:
        logger.warning("fit_logo_image_failed", exc_info=True)
        return None
