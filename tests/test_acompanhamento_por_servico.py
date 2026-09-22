"""Breakdown por serviço / status do acompanhamento saidas-dia."""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://pytest:pytest@localhost/pytest")
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")

from acompanhamento_routes import (  # noqa: E402
    _bucket_status_acompanhamento,
    _por_servico_breakdown,
)


def test_bucket_status_acompanhamento():
    assert _bucket_status_acompanhamento("ENTREGUE") == "entregues"
    assert _bucket_status_acompanhamento("entregue") == "entregues"
    assert _bucket_status_acompanhamento("EM_ROTA") == "pendentes"
    assert _bucket_status_acompanhamento("SAIU_PARA_ENTREGA") == "pendentes"
    assert _bucket_status_acompanhamento("saiu") == "pendentes"
    assert _bucket_status_acompanhamento("AUSENTE") == "ausentes"
    assert _bucket_status_acompanhamento("coletado") is None
    assert _bucket_status_acompanhamento("NA_BASE") is None


def test_por_servico_breakdown_conta_status_por_marketplace():
    rows = [
        SimpleNamespace(servico="Shopee", status="ENTREGUE"),
        SimpleNamespace(servico="Shopee", status="EM_ROTA"),
        SimpleNamespace(servico="Mercado Livre", status="AUSENTE"),
        SimpleNamespace(servico="Avulso", status="SAIU_PARA_ENTREGA"),
        SimpleNamespace(servico="Avulso", status="coletado"),
    ]
    por = _por_servico_breakdown(rows)
    assert por.shopee.total == 2
    assert por.shopee.entregues == 1
    assert por.shopee.pendentes == 1
    assert por.shopee.ausentes == 0
    assert por.mercado_livre.total == 1
    assert por.mercado_livre.ausentes == 1
    assert por.avulso.total == 2
    assert por.avulso.pendentes == 1
    # coletado não entra em pendentes/entregues/ausentes
    assert por.avulso.entregues == 0
    assert por.avulso.ausentes == 0


def test_entradas_uniao_entrada_base_e_estoque():
    """Simula a união usada em /acompanhamento/dia (sem DB)."""
    ids_entrada_base = {1, 2}
    ids_estoque = {2, 3}  # 3 = coletado sem entrada_base
    assert len(ids_entrada_base | ids_estoque) == 3
