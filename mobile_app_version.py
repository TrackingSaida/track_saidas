"""Política de versão mínima do app mobile (Android).

O app consulta esta política e bloqueia o uso quando a versão instalada
está abaixo do piso do perfil. Quem ainda não tem essa checagem no
aplicativo não é afetado: a trava vale a partir do build que a consulta.

Padrão atual:
- todos os perfis: 1.11.0 (melhorias que impactam o fluxo)
- root (0), admin (1) e operador (2): 1.12.0
- coletador (3) e motoboy (4): só o piso geral
"""
from __future__ import annotations

import os
from dataclasses import dataclass

PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=br.com.trackingsaidas.mobile"
DEFAULT_MIN_VERSION = "1.11.0"
DEFAULT_MESSAGE = (
    "Há uma nova versão do ROTEVO. Atualize para continuar usando o aplicativo."
)
# role numérico do JWT
DEFAULT_ROLE_MINIMUMS: dict[int, str] = {
    0: "1.12.0",  # root
    1: "1.12.0",  # admin
    2: "1.12.0",  # operador
}
ROLE_ALIASES = {
    "root": 0,
    "admin": 1,
    "operador": 2,
    "coletador": 3,
    "motoboy": 4,
}


@dataclass(frozen=True)
class AppVersionPolicy:
    min_version: str
    min_version_by_role: dict[int, str]
    store_url: str
    message: str


def parse_version(value: str | None) -> tuple[int, ...] | None:
    text = (value or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    text = text.split("-", 1)[0].split("+", 1)[0].strip()
    if not text:
        return None
    parts: list[int] = []
    for part in text.split("."):
        if not part.isdigit():
            return None
        parts.append(int(part))
    if not parts:
        return None
    return tuple(parts)


def version_lt(left: str | None, right: str | None) -> bool:
    a = parse_version(left)
    b = parse_version(right)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    a_pad = a + (0,) * (width - len(a))
    b_pad = b + (0,) * (width - len(b))
    return a_pad < b_pad


def higher_version(left: str | None, right: str | None) -> str | None:
    if parse_version(left) is None:
        return right if parse_version(right) is not None else None
    if parse_version(right) is None:
        return left
    if version_lt(left, right):
        return right
    return left


def parse_role_minimums(raw: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for chunk in (raw or "").split(","):
        piece = chunk.strip()
        if not piece or ":" not in piece:
            continue
        role_raw, version_raw = piece.split(":", 1)
        role_key = role_raw.strip().lower()
        version = version_raw.strip()
        if parse_version(version) is None:
            continue
        if role_key.isdigit():
            result[int(role_key)] = version
            continue
        alias = ROLE_ALIASES.get(role_key)
        if alias is not None:
            result[alias] = version
    return result


def load_policy() -> AppVersionPolicy:
    min_version = (os.getenv("MOBILE_MIN_VERSION") or DEFAULT_MIN_VERSION).strip()
    if parse_version(min_version) is None:
        min_version = DEFAULT_MIN_VERSION

    raw_roles = os.environ.get("MOBILE_MIN_VERSION_ROLES")
    if raw_roles is None:
        by_role = dict(DEFAULT_ROLE_MINIMUMS)
    else:
        by_role = parse_role_minimums(raw_roles)

    store_url = (os.getenv("MOBILE_PLAY_STORE_URL") or PLAY_STORE_URL).strip() or PLAY_STORE_URL
    message = (os.getenv("MOBILE_UPDATE_MESSAGE") or DEFAULT_MESSAGE).strip() or DEFAULT_MESSAGE
    return AppVersionPolicy(
        min_version=min_version,
        min_version_by_role=by_role,
        store_url=store_url,
        message=message,
    )


def required_version(policy: AppVersionPolicy, role: int | None) -> str | None:
    chosen = policy.min_version if parse_version(policy.min_version) is not None else None
    if role is None:
        return chosen
    role_min = policy.min_version_by_role.get(role)
    return higher_version(chosen, role_min)


def is_update_required(installed: str | None, policy: AppVersionPolicy, role: int | None) -> bool:
    required = required_version(policy, role)
    if required is None or parse_version(installed) is None:
        return False
    return version_lt(installed, required)


def public_policy_payload(policy: AppVersionPolicy | None = None) -> dict:
    current = policy or load_policy()
    return {
        "min_version": current.min_version,
        "min_version_by_role": {
            str(role): version for role, version in sorted(current.min_version_by_role.items())
        },
        "store_url": current.store_url,
        "message": current.message,
    }
