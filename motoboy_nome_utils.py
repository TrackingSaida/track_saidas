"""Helpers centralizados para nome de motoboy/entregador em listagens."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Motoboy, User
from name_normalizer import format_person_full_name, normalize_display_name, person_name_sort_key


def format_motoboy_nome_parts(
    nome: Optional[str],
    sobrenome: Optional[str],
    username: Optional[str] = None,
    *,
    motoboy_id: Optional[int] = None,
) -> str:
    fallback = f"Motoboy {motoboy_id}" if motoboy_id is not None else "Motoboy"
    return format_person_full_name(
        nome,
        sobrenome,
        username=username,
        fallback=fallback,
    )


def get_motoboy_display_name(
    db: Session,
    motoboy_id: Optional[int] = None,
    *,
    motoboy: Optional[Motoboy] = None,
) -> str:
    """Nome normalizado de um motoboy (via User)."""
    mb = motoboy
    mid = motoboy_id
    if mb is None and mid is not None:
        mb = db.get(Motoboy, int(mid))
    if mb is None:
        return f"Motoboy {mid}" if mid is not None else "Motoboy"
    mid = int(mb.id_motoboy)
    if not mb.user_id:
        return f"Motoboy {mid}"
    u = db.get(User, mb.user_id)
    if not u:
        return f"Motoboy {mid}"
    return format_motoboy_nome_parts(
        getattr(u, "nome", None),
        getattr(u, "sobrenome", None),
        getattr(u, "username", None),
        motoboy_id=mid,
    )


def carregar_nomes_motoboy_ids(db: Session, motoboy_ids: Iterable[int]) -> Dict[int, str]:
    """Resolve nomes normalizados de motoboy em lote (evita N+1)."""
    ids = sorted({int(mid) for mid in motoboy_ids if mid is not None})
    if not ids:
        return {}
    rows_motoboy = db.execute(
        select(Motoboy.id_motoboy, Motoboy.user_id).where(Motoboy.id_motoboy.in_(ids))
    ).all()
    motoboy_user_map: Dict[int, Optional[int]] = {
        int(mid): (int(uid) if uid is not None else None)
        for mid, uid in rows_motoboy
    }
    user_ids = sorted({uid for uid in motoboy_user_map.values() if uid is not None})
    user_map: Dict[int, tuple] = {}
    if user_ids:
        rows_user = db.execute(
            select(User.id, User.nome, User.sobrenome, User.username).where(User.id.in_(user_ids))
        ).all()
        user_map = {
            int(uid): ((nome or ""), (sobrenome or ""), (username or ""))
            for uid, nome, sobrenome, username in rows_user
        }
    out: Dict[int, str] = {}
    for mid in ids:
        uid = motoboy_user_map.get(mid)
        if uid is None:
            out[mid] = f"Motoboy {mid}"
            continue
        nome, sobrenome, username = user_map.get(uid, ("", "", ""))
        out[mid] = format_motoboy_nome_parts(
            nome, sobrenome, username, motoboy_id=mid
        )
    return out


def sort_by_person_name(items: List, *, attr: str = "nome") -> List:
    """Ordena lista de objetos/dicts por nome (pt-BR casefold)."""
    def _key(item):
        if isinstance(item, dict):
            return person_name_sort_key(item.get(attr))
        return person_name_sort_key(getattr(item, attr, None))

    return sorted(items, key=_key)


def normalize_entregador_label(value: Optional[str], *, fallback: str = "") -> str:
    """Normaliza label legado de entregador (string livre)."""
    return normalize_display_name(value, fallback=fallback)
