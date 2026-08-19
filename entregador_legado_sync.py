"""
Sincronização do espelho legado (entregador / entregador_preco) com User/Motoboy.

Quando um usuário (especialmente role=4) é inativado ou excluído, o registro
legado em `entregador` + exceção em `entregador_preco` costuma permanecer e
voltar a aparecer em filtros (Fechamento, Registros, etc.).
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Set

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from entregador_legado_pure import entregador_aparece_no_filtro_operacional
from models import Entregador, EntregadorPreco, Motoboy, MotoboySubBase, User


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").strip().lower().split())


def encontrar_entregadores_legados_do_usuario(
    db: Session,
    user: User,
    *,
    motoboy: Optional[Motoboy] = None,
) -> List[Entregador]:
    """Localiza entregadores legado da mesma sub_base vinculados ao usuário."""
    sub_base = (getattr(user, "sub_base", None) or "").strip()
    if not sub_base:
        return []

    if motoboy is None and getattr(user, "motoboy", None) is not None:
        motoboy = user.motoboy
    if motoboy is None and getattr(user, "id", None) is not None:
        motoboy = db.scalars(select(Motoboy).where(Motoboy.user_id == user.id)).first()

    usernames: Set[str] = set()
    documentos: Set[str] = set()
    username = (getattr(user, "username", None) or "").strip()
    if username:
        usernames.add(username)
    if motoboy and (motoboy.documento or "").strip():
        documentos.add(motoboy.documento.strip())

    found: dict[int, Entregador] = {}

    conds = []
    if usernames:
        conds.append(Entregador.username_entregador.in_(sorted(usernames)))
    if documentos:
        conds.append(Entregador.documento.in_(sorted(documentos)))
    if conds:
        rows = db.scalars(
            select(Entregador).where(
                Entregador.sub_base == sub_base,
                or_(*conds),
            )
        ).all()
        for ent in rows:
            found[int(ent.id_entregador)] = ent

    nome_user = _norm(
        f"{(getattr(user, 'nome', None) or '')} {(getattr(user, 'sobrenome', None) or '')}"
    )
    if nome_user and not found:
        for ent in db.scalars(
            select(Entregador).where(Entregador.sub_base == sub_base)
        ).all():
            if _norm(ent.nome) == nome_user:
                found[int(ent.id_entregador)] = ent

    return list(found.values())


def _remover_excecao_preco(db: Session, id_entregador: int) -> None:
    ep = db.scalars(
        select(EntregadorPreco).where(EntregadorPreco.id_entregador == int(id_entregador))
    ).first()
    if ep:
        db.delete(ep)


def sincronizar_legado_entregador_com_status_usuario(
    db: Session,
    user: User,
    *,
    ativo: bool,
    remover_excecao_preco: bool = False,
) -> None:
    """
    Atualiza espelho legado + flags Motoboy conforme status do User.

    - ativo=False: desativa Motoboy/MotoboySubBase/Entregador; opcionalmente remove preço.
    - ativo=True: reativa Motoboy/MotoboySubBase; reativa Entregador se existir.
    """
    motoboy = getattr(user, "motoboy", None)
    if motoboy is None and getattr(user, "id", None) is not None:
        motoboy = db.scalars(select(Motoboy).where(Motoboy.user_id == user.id)).first()

    if motoboy is not None:
        motoboy.ativo = bool(ativo)
        for vinculo in db.scalars(
            select(MotoboySubBase).where(MotoboySubBase.motoboy_id == motoboy.id_motoboy)
        ).all():
            vinculo.ativo = bool(ativo)

    entregadores = encontrar_entregadores_legados_do_usuario(db, user, motoboy=motoboy)
    for ent in entregadores:
        if remover_excecao_preco or not ativo:
            _remover_excecao_preco(db, int(ent.id_entregador))
        ent.ativo = bool(ativo)


def limpar_legado_entregador_ao_excluir_usuario(db: Session, user: User) -> None:
    """Remove exceção de preço e desativa entregador legado antes do hard delete do User."""
    sincronizar_legado_entregador_com_status_usuario(
        db,
        user,
        ativo=False,
        remover_excecao_preco=True,
    )


def _user_nome_norm(user: User) -> str:
    return _norm(f"{getattr(user, 'nome', None) or ''} {getattr(user, 'sobrenome', None) or ''}")


def _indice_motoboys_operacionais(db: Session, sub_base: str) -> tuple[dict[str, User], set[str]]:
    """Mapa username→User da sub_base e nomes de motoboys operacionais ativos."""
    users = list(
        db.scalars(select(User).where(User.sub_base == sub_base)).all()
    )
    by_username: dict[str, User] = {}
    for u in users:
        un_raw = (getattr(u, "username", None) or "").strip()
        if un_raw and un_raw not in by_username:
            by_username[un_raw] = u
        un = _norm(un_raw)
        if un and un not in by_username:
            by_username[un] = u

    nomes_ativos: set[str] = set()
    motoboys = list(
        db.scalars(
            select(User)
            .join(Motoboy, Motoboy.user_id == User.id)
            .join(MotoboySubBase, MotoboySubBase.motoboy_id == Motoboy.id_motoboy)
            .where(
                MotoboySubBase.sub_base == sub_base,
                MotoboySubBase.ativo.is_(True),
                Motoboy.ativo.is_(True),
                User.status.is_(True),
                User.role == 4,
            )
        ).all()
    )
    for u in motoboys:
        nome = _user_nome_norm(u)
        if nome:
            nomes_ativos.add(nome)
    for u in users:
        if int(getattr(u, "role", 0) or 0) != 4:
            continue
        if not bool(getattr(u, "status", True)):
            continue
        nome = _user_nome_norm(u)
        if nome:
            nomes_ativos.add(nome)
    return by_username, nomes_ativos


def entregador_tem_usuario_operacional_ativo(db: Session, ent: Entregador) -> bool:
    """
    True se o entregador pode aparecer em filtros operacionais (ex.: Fechamento).

    - Com username: exige User na mesma sub_base com status=True.
    - Sem username: só entra se ainda existir motoboy ativo com o mesmo nome.
      Órfão de usuário excluído não entra no dropdown.
    """
    sub_base = (ent.sub_base or "").strip()
    if not sub_base:
        return False
    by_username, nomes_ativos = _indice_motoboys_operacionais(db, sub_base)
    username = (ent.username_entregador or "").strip()
    user = (by_username.get(username) or by_username.get(_norm(username))) if username else None
    user_ativo: Optional[bool] = None if user is None else bool(getattr(user, "status", True))
    return entregador_aparece_no_filtro_operacional(
        username=username,
        user_por_username_ativo=user_ativo,
        nome_tem_user_ativo=_norm(ent.nome) in nomes_ativos,
    )


def filtrar_entregadores_operacionais(
    db: Session,
    entregadores: Iterable[Entregador],
) -> List[Entregador]:
    ents = list(entregadores)
    if not ents:
        return []
    sub_base = (ents[0].sub_base or "").strip()
    if not sub_base:
        return []
    by_username, nomes_ativos = _indice_motoboys_operacionais(db, sub_base)
    out: List[Entregador] = []
    for ent in ents:
        username = (ent.username_entregador or "").strip()
        user = (by_username.get(username) or by_username.get(_norm(username))) if username else None
        user_ativo: Optional[bool] = None if user is None else bool(getattr(user, "status", True))
        if entregador_aparece_no_filtro_operacional(
            username=username,
            user_por_username_ativo=user_ativo,
            nome_tem_user_ativo=_norm(ent.nome) in nomes_ativos,
        ):
            out.append(ent)
    return out
