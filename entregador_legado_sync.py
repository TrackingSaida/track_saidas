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


def entregador_tem_usuario_operacional_ativo(db: Session, ent: Entregador) -> bool:
    """
    True se o entregador pode aparecer em filtros operacionais.

    - Sem username: trata como legado puro (mantém se ativo).
    - Com username: exige User na mesma sub_base com status=True.
    - Se o User existir e estiver inativo, ou não existir (excluído), retorna False.
    """
    username = (ent.username_entregador or "").strip()
    if not username:
        return True

    sub_base = (ent.sub_base or "").strip()
    user = db.scalars(
        select(User).where(
            User.sub_base == sub_base,
            User.username == username,
        )
    ).first()
    if user is None:
        # Espelho de usuário já excluído (ou username órfão).
        return False
    return bool(getattr(user, "status", True))


def filtrar_entregadores_operacionais(
    db: Session,
    entregadores: Iterable[Entregador],
) -> List[Entregador]:
    return [e for e in entregadores if entregador_tem_usuario_operacional_ativo(db, e)]
