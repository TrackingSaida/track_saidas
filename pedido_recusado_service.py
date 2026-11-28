"""Fila e push de pedidos ausentes com motivo Recusado (admin/root)."""
from __future__ import annotations

import logging
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from models import User

logger = logging.getLogger(__name__)

MOTIVO_RECUSADO_CANONICO = "recusado"


def coerce_admin_root_role(raw: Any) -> Optional[int]:
    """Role 0 é falsy em Python — não usar `or`."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def require_admin_root_sub_base(user: Any) -> str:
    """Garante papéis 0/1 e sub_base do token. Levanta HTTPException 403."""
    from fastapi import HTTPException

    role = coerce_admin_root_role(getattr(user, "role", None))
    if role not in (0, 1):
        raise HTTPException(status_code=403, detail="Apenas admin ou root.")
    sub_base = (getattr(user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")
    return sub_base


def normalizar_texto_motivo(raw: Optional[str]) -> str:
    s = unicodedata.normalize("NFD", (raw or "").strip().lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def is_motivo_recusado(descricao: Optional[str]) -> bool:
    return normalizar_texto_motivo(descricao) == MOTIVO_RECUSADO_CANONICO


def listar_pedidos_recusados(
    db: "Session",
    *,
    sub_base: str,
) -> List[Dict[str, Any]]:
    """Ausentes atuais da sub_base com motivo exatamente Recusado."""
    from sqlalchemy import select

    from models import Saida, SaidaDetail
    from saidas_routes import STATUS_AUSENTE

    sb = (sub_base or "").strip()
    if not sb:
        return []
    q = (
        select(Saida, SaidaDetail)
        .outerjoin(SaidaDetail, SaidaDetail.id_saida == Saida.id_saida)
        .where(
            Saida.sub_base == sb,
            Saida.codigo.isnot(None),
            Saida.status == STATUS_AUSENTE,
        )
        .order_by(Saida.data.desc(), Saida.timestamp.desc())
    )
    rows = db.execute(q).all()
    items: List[Dict[str, Any]] = []
    for saida, detail in rows:
        motivo = getattr(detail, "motivo_ocorrencia", None) if detail else None
        if not is_motivo_recusado(motivo):
            continue
        items.append(
            {
                "id_saida": int(saida.id_saida),
                "codigo": (saida.codigo or "").strip() or None,
                "codigo_exibicao": None,
                "servico": (saida.servico or "").strip() or None,
                "base": (getattr(saida, "base", None) or "").strip() or None,
            }
        )
    if items:
        from types import SimpleNamespace

        from avulso_campos_service import labels_exibicao_por_saidas

        row_ns = []
        for it in items:
            cod = str(it.get("codigo") or "")
            srv = str(it.get("servico") or "").lower()
            if not (cod.upper().startswith("AVULSO-") or "avulso" in srv):
                continue
            row_ns.append(
                SimpleNamespace(
                    id_saida=int(it["id_saida"]),
                    codigo=cod,
                    base=it.get("base"),
                )
            )
        if row_ns:
            labels = labels_exibicao_por_saidas(db, sub_base=sb, rows=row_ns)
            for it in items:
                label = labels.get(int(it["id_saida"]))
                if label:
                    it["codigo_exibicao"] = label
    return items


def contar_pedidos_recusados(db: "Session", *, sub_base: str) -> int:
    return len(listar_pedidos_recusados(db, sub_base=sub_base))


def notificar_staff_pedidos_recusados(
    db: "Session",
    *,
    sub_base: str,
    ids_saida: Sequence[int],
    marcado_por: Optional[str] = None,
) -> int:
    """
    Push para root/admin da sub_base. Dedupe por ids da ação (retry da mesma baixa não avisa de novo).
    """
    ids = [int(i) for i in ids_saida if i is not None]
    if not ids or not (sub_base or "").strip():
        return 0
    from push_notification_service import send_to_staff_sub_base

    n = len(ids)
    quem = (marcado_por or "").strip()
    if n == 1:
        title = "1 pedido recusado"
    else:
        title = f"{n} pedidos recusados"
    if quem:
        body = f"{title} — marcado por {quem}."
    else:
        body = f"{title}. Abra para ver e cancelar na consulta."

    enviados = 0
    chave = ",".join(str(i) for i in sorted(ids))
    try:
        enviados = send_to_staff_sub_base(
            db,
            sub_base=sub_base,
            tipo="pedido_recusado",
            title=title,
            body=body,
            data={
                "type": "pedido_recusado",
                "tipo": "pedido_recusado",
                "count": n,
                "ids": ids,
                "screen": "pedidos_recusados",
            },
            roles=(0, 1),
            chave_dedupe=chave,
        )
    except Exception:
        logger.exception("push_pedido_recusado_failed sub_base=%s ids=%s", sub_base, ids)
    return enviados


def talvez_notificar_recusado_apos_ausencia(
    db: "Session",
    *,
    sub_base: str,
    user: "User",
    ids_e_motivos: Iterable[tuple[int, Optional[str]]],
) -> None:
    """Filtra só Recusado e dispara push. Best-effort após commit da ausência."""
    ids = [int(sid) for sid, motivo in ids_e_motivos if is_motivo_recusado(motivo)]
    if not ids:
        return
    marcado_por = (getattr(user, "username", None) or "").strip() or None
    notificar_staff_pedidos_recusados(
        db,
        sub_base=sub_base,
        ids_saida=ids,
        marcado_por=marcado_por,
    )
    try:
        db.commit()
    except Exception:
        logger.exception("commit_push_pedido_recusado_failed")
