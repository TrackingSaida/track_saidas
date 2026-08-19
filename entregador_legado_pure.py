"""Regras puras do filtro operacional de entregador legado (sem SQLAlchemy)."""
from __future__ import annotations

from typing import Optional


def entregador_aparece_no_filtro_operacional(
    *,
    username: Optional[str],
    user_por_username_ativo: Optional[bool],
    nome_tem_user_ativo: bool,
) -> bool:
    """Decide se um entregador legado entra no dropdown operacional (fechamento, etc.).

    - Com username: só entra se existir User na sub_base com status ativo.
      User ausente (excluído) ou inativo → False.
    - Sem username: só entra se ainda existir User motoboy ativo com o mesmo nome.
      Caso contrário é órfão de exclusão (hard delete) ou legado sem titular.
    """
    if (username or "").strip():
        return user_por_username_ativo is True
    return bool(nome_tem_user_ativo)
