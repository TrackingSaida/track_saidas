from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from pedido_recusado_service import (
    coerce_admin_root_role,
    is_motivo_recusado,
    normalizar_texto_motivo,
    require_admin_root_sub_base,
)


def test_normalizar_motivo_recusado():
    assert normalizar_texto_motivo("Recusado") == "recusado"
    assert normalizar_texto_motivo("  RECUSADO ") == "recusado"
    assert is_motivo_recusado("Recusado") is True
    assert is_motivo_recusado("Cliente ausente") is False
    assert is_motivo_recusado("pedido recusado pelo cliente") is False


def test_coerce_admin_root_role_zero_nao_e_falsy():
    assert coerce_admin_root_role(0) == 0
    assert coerce_admin_root_role("0") == 0
    assert coerce_admin_root_role(1) == 1
    assert coerce_admin_root_role(None) is None


def test_require_admin_root_aceita_role_zero():
    """Root (role=0) não pode ser bloqueado por `or` falsy."""
    user = SimpleNamespace(role=0, sub_base="base-a")
    assert require_admin_root_sub_base(user) == "base-a"

    with pytest.raises(HTTPException) as exc:
        require_admin_root_sub_base(SimpleNamespace(role=2, sub_base="base-a"))
    assert exc.value.status_code == 403
