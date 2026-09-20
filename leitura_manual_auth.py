"""Autorização de digitação manual de códigos."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import HTTPException
from sqlalchemy import select, update as sa_update
from sqlalchemy.orm import Session, joinedload

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


def _explicit_bool(obj: object, attr: str) -> Optional[bool]:
    if obj is None or not hasattr(obj, attr):
        return None
    val = getattr(obj, attr)
    if val is None:
        return None
    return bool(val)


def motoboy_pode_criar_avulso(motoboy: Motoboy, contexto: AvulsoContexto) -> bool:
    legado = _explicit_bool(motoboy, "pode_lancar_avulso")
    if contexto == "coleta":
        flag = _explicit_bool(motoboy, "pode_criar_avulso_coleta")
    else:
        flag = _explicit_bool(motoboy, "pode_criar_avulso_saida")
    if flag is not None:
        return flag
    return True if legado is None else legado


def resolve_owner_avulso_defaults(owner: Owner) -> tuple[bool, bool]:
    """Coleta/saída independentes. False persistido não cai no legado OR."""
    legado = _explicit_bool(owner, "default_pode_lancar_avulso")
    coleta = _explicit_bool(owner, "default_pode_criar_avulso_coleta")
    saida = _explicit_bool(owner, "default_pode_criar_avulso_saida")
    if coleta is None:
        coleta = True if legado is None else legado
    if saida is None:
        saida = True if legado is None else legado
    return bool(coleta), bool(saida)


def apply_owner_avulso_padroes(
    owner: Owner,
    *,
    pode_criar_avulso_coleta: Optional[bool] = None,
    pode_criar_avulso_saida: Optional[bool] = None,
    pode_lancar_avulso: Optional[bool] = None,
) -> tuple[bool, bool]:
    coleta, saida = resolve_owner_avulso_defaults(owner)
    if pode_criar_avulso_coleta is not None or pode_criar_avulso_saida is not None:
        if pode_criar_avulso_coleta is not None:
            coleta = bool(pode_criar_avulso_coleta)
        if pode_criar_avulso_saida is not None:
            saida = bool(pode_criar_avulso_saida)
    elif pode_lancar_avulso is not None:
        coleta = saida = bool(pode_lancar_avulso)
    owner.default_pode_criar_avulso_coleta = coleta
    owner.default_pode_criar_avulso_saida = saida
    sync_owner_avulso_defaults(owner)
    _flag_modified_if_mapped(
        owner,
        "default_pode_criar_avulso_coleta",
        "default_pode_criar_avulso_saida",
        "default_pode_lancar_avulso",
        "default_avulso_exige_foto",
    )
    return coleta, saida


def apply_motoboy_avulso_padroes(
    motoboy: Motoboy,
    *,
    pode_criar_avulso_coleta: bool,
    pode_criar_avulso_saida: bool,
) -> None:
    motoboy.pode_criar_avulso_coleta = bool(pode_criar_avulso_coleta)
    motoboy.pode_criar_avulso_saida = bool(pode_criar_avulso_saida)
    sync_motoboy_avulso_legado(motoboy)
    _flag_modified_if_mapped(
        motoboy,
        "pode_criar_avulso_coleta",
        "pode_criar_avulso_saida",
        "pode_lancar_avulso",
    )


def resolve_motoboy_avulso_flags(motoboy: Motoboy) -> tuple[bool, bool]:
    legado = _explicit_bool(motoboy, "pode_lancar_avulso")
    coleta = _explicit_bool(motoboy, "pode_criar_avulso_coleta")
    saida = _explicit_bool(motoboy, "pode_criar_avulso_saida")
    if coleta is None:
        coleta = True if legado is None else legado
    if saida is None:
        saida = True if legado is None else legado
    return bool(coleta), bool(saida)


def list_users_role4_da_sub_base(db: Session, sub_base: str) -> list[User]:
    """Mesma população de Usuários: User.sub_base + role=4, com perfil motoboy se houver."""
    sub = (sub_base or "").strip()
    if not sub:
        return []
    stmt = (
        select(User)
        .options(joinedload(User.motoboy))
        .where(User.sub_base == sub, User.role == 4)
        .order_by(User.id.asc())
    )
    return list(db.scalars(stmt).unique().all())


def list_motoboys_da_sub_base(db: Session, sub_base: str) -> list[Motoboy]:
    """Motoboys visíveis em Usuários (User.sub_base + role=4 com linha em motoboys)."""
    out: list[Motoboy] = []
    seen: set[int] = set()
    for u in list_users_role4_da_sub_base(db, sub_base):
        m = getattr(u, "motoboy", None)
        if m is None:
            continue
        mid = int(getattr(m, "id_motoboy", 0) or 0)
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        out.append(m)
    return out


def flush_owner_avulso_columns(db: Session, owner: Owner) -> tuple[bool, bool]:
    """UPDATE explícito das colunas de avulso do owner (False precisa ir ao SQL)."""
    coleta, saida = resolve_owner_avulso_defaults(owner)
    legado = bool(coleta or saida)
    owner.default_pode_criar_avulso_coleta = coleta
    owner.default_pode_criar_avulso_saida = saida
    owner.default_pode_lancar_avulso = legado
    if not legado:
        owner.default_avulso_exige_foto = False
    oid = getattr(owner, "id_owner", None)
    execute = getattr(db, "execute", None)
    if oid is None or not callable(execute):
        return coleta, saida
    foto = bool(getattr(owner, "default_avulso_exige_foto", False))
    execute(
        sa_update(Owner)
        .where(Owner.id_owner == int(oid))
        .values(
            default_pode_criar_avulso_coleta=coleta,
            default_pode_criar_avulso_saida=saida,
            default_pode_lancar_avulso=legado,
            default_avulso_exige_foto=foto,
        )
    )
    return coleta, saida


def flush_motoboy_avulso_columns(db: Session, motoboy: Motoboy) -> None:
    """UPDATE explícito das flags do motoboy para o SQL não omitir False."""
    mid = getattr(motoboy, "id_motoboy", None)
    execute = getattr(db, "execute", None)
    if mid is None or not callable(execute):
        return
    execute(
        sa_update(Motoboy)
        .where(Motoboy.id_motoboy == int(mid))
        .values(
            pode_criar_avulso_coleta=bool(getattr(motoboy, "pode_criar_avulso_coleta", False)),
            pode_criar_avulso_saida=bool(getattr(motoboy, "pode_criar_avulso_saida", False)),
            pode_lancar_avulso=bool(getattr(motoboy, "pode_lancar_avulso", False)),
            avulso_exige_foto=bool(getattr(motoboy, "avulso_exige_foto", False)),
            pode_realizar_coleta=bool(getattr(motoboy, "pode_realizar_coleta", False)),
            pode_ler_coleta=bool(getattr(motoboy, "pode_ler_coleta", False)),
            pode_ler_saida=bool(getattr(motoboy, "pode_ler_saida", True)),
            pode_digitar_codigo_manual=bool(getattr(motoboy, "pode_digitar_codigo_manual", False)),
            claims_version=int(getattr(motoboy, "claims_version", 0) or 0),
        )
    )


def _flag_modified_if_mapped(obj: object, *attrs: str) -> None:
    try:
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.orm.attributes import flag_modified

        insp = sa_inspect(obj, raiseerr=False)
        if insp is None or getattr(insp, "mapper", None) is None:
            return
        mapped = insp.mapper.attrs
        for attr in attrs:
            if attr in mapped:
                flag_modified(obj, attr)
    except Exception:
        return


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
