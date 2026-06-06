from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Set

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from codigo_normalizer import canonicalize_servico
from models import PedidoCamposObrigatoriosConfig, Saida, SaidaDetail

CONTEXTOS_VALIDOS = {"ENTREGUE", "AUSENTE", "AMBOS"}
CAMPOS_VALIDOS = {"foto", "recebedor", "tipo_recebedor", "documento", "observacao"}
CAMPO_LABEL: Dict[str, str] = {
    "foto": "Foto",
    "recebedor": "Recebedor",
    "tipo_recebedor": "Tipo Recebedor",
    "documento": "Documento",
    "observacao": "Observação",
}


def normalize_contexto(raw: str) -> str:
    contexto = (raw or "").strip().upper()
    if contexto not in CONTEXTOS_VALIDOS:
        raise HTTPException(status_code=422, detail="contexto inválido. Use ENTREGUE, AUSENTE ou AMBOS.")
    return contexto


def normalize_campos_obrigatorios(campos: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for campo in campos or []:
        c = (campo or "").strip().lower()
        if not c:
            continue
        if c not in CAMPOS_VALIDOS:
            raise HTTPException(status_code=422, detail=f"Campo obrigatório inválido: {campo}")
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def parse_campos_obrigatorios_text(raw: Optional[str]) -> List[str]:
    try:
        parsed = json.loads((raw or "[]").strip() or "[]")
        if not isinstance(parsed, list):
            return []
        return [str(c).strip().lower() for c in parsed if str(c).strip().lower() in CAMPOS_VALIDOS]
    except Exception:
        return []


def resolve_campos_obrigatorios_ativos(
    db: Session,
    *,
    sub_base: str,
    servico: Optional[str],
    contexto: str,
) -> List[str]:
    contexto_norm = normalize_contexto(contexto)
    servico_norm = canonicalize_servico(servico)
    rows = db.scalars(
        select(PedidoCamposObrigatoriosConfig).where(
            PedidoCamposObrigatoriosConfig.sub_base == sub_base,
            PedidoCamposObrigatoriosConfig.servico == servico_norm,
            PedidoCamposObrigatoriosConfig.ativo.is_(True),
            PedidoCamposObrigatoriosConfig.contexto.in_([contexto_norm, "AMBOS"]),
        )
    ).all()
    campos_union: Set[str] = set()
    for row in rows:
        for c in parse_campos_obrigatorios_text(getattr(row, "campos_obrigatorios", "[]")):
            campos_union.add(c)
    return sorted(campos_union)


def build_campos_cache_for_sub_base(db: Session, *, sub_base: str) -> Dict[tuple, List[str]]:
    cache: Dict[tuple, List[str]] = {}
    rows = db.scalars(
        select(PedidoCamposObrigatoriosConfig).where(
            PedidoCamposObrigatoriosConfig.sub_base == sub_base,
            PedidoCamposObrigatoriosConfig.ativo.is_(True),
        )
    ).all()
    for row in rows:
        servico = canonicalize_servico(getattr(row, "servico", None))
        contexto = normalize_contexto(getattr(row, "contexto", "AMBOS"))
        campos = parse_campos_obrigatorios_text(getattr(row, "campos_obrigatorios", "[]"))
        key = (servico, contexto)
        if key not in cache:
            cache[key] = []
        cache[key] = sorted(set(cache[key] + campos))
    return cache


def resolve_campos_obrigatorios_from_cache(
    *,
    cache: Dict[tuple, List[str]],
    servico: Optional[str],
    contexto: str,
) -> List[str]:
    servico_norm = canonicalize_servico(servico)
    contexto_norm = normalize_contexto(contexto)
    merged = set(cache.get((servico_norm, "AMBOS"), [])) | set(cache.get((servico_norm, contexto_norm), []))
    return sorted(merged)


def _has_foto(detail: Optional[SaidaDetail], overrides: Optional[Dict[str, Optional[str]]] = None) -> bool:
    if overrides and "foto_url" in overrides:
        value = (overrides.get("foto_url") or "").strip()
    else:
        value = (getattr(detail, "foto_url", None) or "").strip() if detail else ""
    if not value:
        return False
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return any((str(v).strip() for v in parsed))
        except Exception:
            pass
    return True


def _pick_value(field_name: str, detail: Optional[SaidaDetail], overrides: Optional[Dict[str, Optional[str]]]) -> str:
    if overrides and field_name in overrides:
        return (overrides.get(field_name) or "").strip()
    return (getattr(detail, field_name, None) or "").strip() if detail else ""


def validate_campos_obrigatorios_conclusao(
    db: Session,
    *,
    saida: Saida,
    contexto: str,
    detail: Optional[SaidaDetail],
    overrides: Optional[Dict[str, Optional[str]]] = None,
) -> List[str]:
    sub_base = (getattr(saida, "sub_base", None) or "").strip()
    if not sub_base:
        return []
    obrigatorios = resolve_campos_obrigatorios_ativos(
        db,
        sub_base=sub_base,
        servico=getattr(saida, "servico", None),
        contexto=contexto,
    )
    faltantes: List[str] = []
    for campo in obrigatorios:
        if campo == "foto":
            if not _has_foto(detail, overrides=overrides):
                faltantes.append(campo)
            continue
        if campo == "recebedor":
            if not _pick_value("nome_recebedor", detail, overrides):
                faltantes.append(campo)
            continue
        if campo == "tipo_recebedor":
            if not _pick_value("tipo_recebedor", detail, overrides):
                faltantes.append(campo)
            continue
        if campo == "documento":
            if not _pick_value("numero_documento", detail, overrides):
                faltantes.append(campo)
            continue
        if campo == "observacao":
            field_name = "observacao_entrega" if contexto == "ENTREGUE" else "observacao_ocorrencia"
            if not _pick_value(field_name, detail, overrides):
                faltantes.append(campo)
            continue
    return faltantes


BLOQUEIO_MOTIVO: Dict[str, str] = {
    "recebedor": "Exige nome do recebedor",
    "tipo_recebedor": "Exige tipo do recebedor",
    "documento": "Exige documento",
    "foto": "Exige foto/comprovante",
    "observacao": "Exige observação",
}


def format_bloqueio_motivo(faltantes: List[str]) -> str:
    if not faltantes:
        return "Campos obrigatórios não preenchidos"
    first = faltantes[0]
    return BLOQUEIO_MOTIVO.get(first, f"Exige {CAMPO_LABEL.get(first, first).lower()}")


def raise_if_campos_obrigatorios_faltando(faltantes: List[str]) -> None:
    if not faltantes:
        return
    labels = [CAMPO_LABEL.get(c, c) for c in faltantes]
    raise HTTPException(
        status_code=422,
        detail={
            "code": "CAMPOS_OBRIGATORIOS_FALTANDO",
            "campos_faltantes": faltantes,
            "message": f"Preencha os campos obrigatórios para concluir este pedido: {', '.join(labels)}.",
        },
    )
