"""Autorização de digitação manual de códigos."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Motoboy, Owner, User

ORIGENS_LEITURA = ("camera", "manual", "selecao")
AvulsoContexto = Literal["coleta", "saida"]


def normalize_origem_leitura(origem: Optional[str], *, default: str = "camera") -> str:
    value = (origem or default or "camera").strip().lower()
    if value not in ORIGENS_LEITURA:
        return default
    return value


def raise_if_selecao_sem_registro(origem: str) -> None:
    """Seleção na lista só associa pacote existente; nunca cria."""
    if origem != "selecao":
        return
    raise HTTPException(
        status_code=404,
        detail={
            "code": "AVULSO_NAO_ENCONTRADO",
            "message": "Avulso não encontrado. Selecione um item da lista ou leia a etiqueta.",
        },
    )


def ensure_manual_code_entry_allowed(
    db: Session,
    user: User,
    *,
    origem: Optional[str],
) -> str:
    """
    Staff (roles 0-3) pode digitar. Motoboy (role 4) só com flag no banco.
    Revalida no DB para permitir revogação sem esperar expirar JWT.
    """
    origem_norm = normalize_origem_leitura(origem)
    role = int(getattr(user, "role", 0) or 0)
    if origem_norm != "manual" or role != 4:
        return origem_norm

    motoboy_id = getattr(user, "motoboy_id", None)
    if not motoboy_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MANUAL_CODE_ENTRY_FORBIDDEN",
                "message": "Digitar código manualmente não é permitido para este perfil.",
            },
        )

    motoboy = db.get(Motoboy, int(motoboy_id))
    if not motoboy or not bool(getattr(motoboy, "pode_digitar_codigo_manual", True)):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MANUAL_CODE_ENTRY_FORBIDDEN",
                "message": "Digitar código manualmente não é permitido para este entregador.",
            },
        )
    return origem_norm


def motoboy_pode_criar_avulso(motoboy: Motoboy, contexto: AvulsoContexto) -> bool:
    if contexto == "coleta":
        return bool(getattr(motoboy, "pode_criar_avulso_coleta", getattr(motoboy, "pode_lancar_avulso", True)))
    return bool(getattr(motoboy, "pode_criar_avulso_saida", getattr(motoboy, "pode_lancar_avulso", True)))


def sync_motoboy_avulso_legado(motoboy: Motoboy) -> None:
    motoboy.pode_lancar_avulso = bool(
        getattr(motoboy, "pode_criar_avulso_coleta", False)
        or getattr(motoboy, "pode_criar_avulso_saida", False)
    )
    if not motoboy.pode_lancar_avulso:
        motoboy.avulso_exige_foto = False


def sync_owner_avulso_defaults(owner: Owner) -> None:
    owner.default_pode_lancar_avulso = bool(
        getattr(owner, "default_pode_criar_avulso_coleta", False)
        or getattr(owner, "default_pode_criar_avulso_saida", False)
    )
    if not owner.default_pode_lancar_avulso:
        owner.default_avulso_exige_foto = False


def ensure_lancar_avulso_allowed(
    db: Session,
    user: User,
    *,
    contexto: AvulsoContexto = "saida",
) -> None:
    """
    Staff (roles 0-3) sempre pode. Motoboy (role 4) só com flag do fluxo.
    Revalida no DB para permitir revogação sem esperar expirar JWT.
    """
    role = int(getattr(user, "role", 0) or 0)
    if role != 4:
        return

    motoboy_id = getattr(user, "motoboy_id", None)
    if not motoboy_id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "LANCAR_AVULSO_FORBIDDEN",
                "message": "Lançar avulso não é permitido para este perfil.",
            },
        )

    motoboy = db.get(Motoboy, int(motoboy_id))
    if not motoboy or not motoboy_pode_criar_avulso(motoboy, contexto):
        fluxo = "coleta" if contexto == "coleta" else "saída"
        raise HTTPException(
            status_code=403,
            detail={
                "code": "LANCAR_AVULSO_FORBIDDEN",
                "message": f"Lançar avulso na {fluxo} não é permitido para este entregador.",
            },
        )
