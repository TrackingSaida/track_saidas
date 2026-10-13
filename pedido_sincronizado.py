# pedido_sincronizado.py - Pedidos importados das integrações (ML/Shopee) e regra de cobrança
"""
Pedido sincronizado só é cobrado (fechamento do seller e cobrança do owner) depois de coletado,
isto é, associado a uma coleta (Saida.id_coleta). Nunca coletado = não entra em cobrança,
mesmo que depois seja cancelado, marcado como G ou saia para entrega.
"""
from __future__ import annotations

from sqlalchemy import and_, exists

from models import Saida, SaidaHistorico

EVENTOS_SYNC_INTEGRACAO = ("ml_int_sync", "shopee_int_sync")
STATUS_AGUARDANDO_COLETA = "aguardando_coleta"

# Fragmento SQL para consultas textuais com a tabela saidas apelidada de "s".
SQL_SINCRONIZADO_NAO_COLETADO = (
    "(s.id_coleta IS NULL AND EXISTS (SELECT 1 FROM saida_historico hs "
    "WHERE hs.id_saida = s.id_saida AND hs.evento IN ('ml_int_sync', 'shopee_int_sync')))"
)


def sincronizado_nao_coletado():
    """Expressão SQLAlchemy: saída importada de integração que nunca foi associada a uma coleta."""
    return and_(
        Saida.id_coleta.is_(None),
        exists().where(
            SaidaHistorico.id_saida == Saida.id_saida,
            SaidaHistorico.evento.in_(EVENTOS_SYNC_INTEGRACAO),
        ),
    )


def aguardando_coleta(status) -> bool:
    return (status or "").strip().lower() == STATUS_AGUARDANDO_COLETA
