"""Serviço de endereços conhecidos (histórico operacional)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from address_fuzzy import find_did_you_mean, similarity
from address_normalizer import normalize_address_key, normalize_street_part, normalizeAddressQuery
from address_providers.base import RawAddressHit
from address_normalizer import normalize_cep
from models import EnderecoConhecido

logger = logging.getLogger(__name__)


def _row_to_hit(row: EnderecoConhecido) -> RawAddressHit:
    return RawAddressHit(
        rua=row.rua or "",
        numero=str(row.numero or ""),
        bairro=row.bairro or "",
        cidade=row.cidade or "",
        estado=(row.estado or "")[:2].upper(),
        cep=normalize_cep(row.cep),
        latitude=float(row.latitude),
        longitude=float(row.longitude),
        source="known",
        external_id=str(row.id),
    )


def search_known(
    db: Session,
    sub_base: str,
    query: str,
    limit: int = 5,
) -> List[Tuple[RawAddressHit, int]]:
    q_norm = normalizeAddressQuery(query)
    q_street = normalize_street_part(q_norm)
    if len(q_street) < 3:
        return []
    pattern = f"%{q_street[:40]}%"
    try:
        rows = (
            db.execute(
                select(EnderecoConhecido)
                .where(
                    EnderecoConhecido.sub_base == sub_base,
                    or_(
                        EnderecoConhecido.rua.ilike(pattern),
                        EnderecoConhecido.bairro.ilike(pattern),
                    ),
                )
                .order_by(EnderecoConhecido.qtd_utilizacoes.desc(), EnderecoConhecido.ultima_utilizacao.desc())
                .limit(limit * 3)
            )
            .scalars()
            .all()
        )
    except Exception as e:
        logger.warning("search_known failed: %s", e)
        return []

    results: List[Tuple[RawAddressHit, int]] = []
    for row in rows:
        sim = similarity(q_street, row.rua or "")
        if sim < 0.5 and q_street not in normalize_street_part(row.rua or ""):
            continue
        results.append((_row_to_hit(row), int(row.qtd_utilizacoes or 1)))
    results.sort(key=lambda x: (-x[1], -similarity(q_street, x[0].rua)))
    return results[:limit]


def upsert_from_save(
    db: Session,
    sub_base: str,
    motoboy_id: Optional[int],
    rua: str,
    numero: str,
    bairro: str,
    cidade: str,
    estado: str,
    cep: str,
    latitude: float,
    longitude: float,
) -> None:
    key_rua = normalize_street_part(rua)
    key_num = normalize_address_key(rua, numero, cep)
    cep_n = normalize_cep(cep)
    try:
        rows = (
            db.execute(
                select(EnderecoConhecido).where(
                    EnderecoConhecido.sub_base == sub_base,
                    EnderecoConhecido.rua.ilike(key_rua[:80] if key_rua else rua),
                )
            )
            .scalars()
            .all()
        )
        match = None
        for row in rows:
            if normalize_address_key(row.rua, row.numero, row.cep) == key_num:
                match = row
                break
        now = datetime.now(timezone.utc)
        if match:
            match.qtd_utilizacoes = (match.qtd_utilizacoes or 0) + 1
            match.ultima_utilizacao = now
            match.latitude = latitude
            match.longitude = longitude
            match.motoboy_id = motoboy_id
            if bairro:
                match.bairro = bairro
        else:
            db.add(
                EnderecoConhecido(
                    sub_base=sub_base,
                    motoboy_id=motoboy_id,
                    rua=rua.strip(),
                    numero=(numero or "").strip() or None,
                    bairro=(bairro or "").strip() or None,
                    cidade=cidade.strip(),
                    estado=(estado or "")[:2].upper(),
                    cep=cep_n or None,
                    latitude=latitude,
                    longitude=longitude,
                    qtd_utilizacoes=1,
                    ultima_utilizacao=now,
                )
            )
        db.commit()
    except Exception as e:
        logger.warning("upsert_from_save failed: %s", e)
        db.rollback()


def get_fuzzy_candidates(db: Session, sub_base: str, limit: int = 100) -> List[Tuple[str, str, str]]:
    try:
        rows = (
            db.execute(
                select(EnderecoConhecido.rua, EnderecoConhecido.cidade, EnderecoConhecido.estado)
                .where(EnderecoConhecido.sub_base == sub_base)
                .order_by(EnderecoConhecido.qtd_utilizacoes.desc())
                .limit(limit)
            )
            .all()
        )
        return [(r[0], r[1], r[2]) for r in rows if r[0]]
    except Exception:
        return []


def build_did_you_mean(db: Session, sub_base: str, query: str) -> Optional[dict]:
    candidates = get_fuzzy_candidates(db, sub_base)
    match = find_did_you_mean(query, candidates)
    if not match:
        return None
    rua, cidade, estado, _sim = match
    rows = (
        db.execute(
            select(EnderecoConhecido)
            .where(
                EnderecoConhecido.sub_base == sub_base,
                EnderecoConhecido.cidade.ilike(cidade),
            )
            .order_by(EnderecoConhecido.qtd_utilizacoes.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    for row in rows:
        if similarity(query, row.rua or "") >= 0.82:
            hit = _row_to_hit(row)
            return {
                "original_query": query,
                "suggestion": _format_suggestion_dict(hit, 90, 0.9, int(row.qtd_utilizacoes or 1)),
            }
    return None


def _format_suggestion_dict(hit: RawAddressHit, score: int, confidence: float, qtd: int) -> dict:
    badge = None
    already_used = qtd > 0
    if qtd >= 10:
        badge = "frequente"
    elif already_used:
        badge = "used"
    label = format_suggestion_label(hit)
    return {
        "label": label,
        "rua": hit.rua,
        "numero": hit.numero,
        "bairro": hit.bairro,
        "cidade": hit.cidade,
        "estado": hit.estado,
        "cep": hit.cep,
        "latitude": hit.latitude,
        "longitude": hit.longitude,
        "score": score,
        "confidence": confidence,
        "source": hit.source,
        "distance_km": None,
        "badge": badge,
        "already_used": already_used,
    }


def format_suggestion_label(hit: RawAddressHit, distance_km: Optional[float] = None) -> str:
    line1_parts = [p for p in [hit.rua, hit.numero] if p]
    line1 = ", ".join(line1_parts) if line1_parts else hit.rua
    lines = [line1]
    if hit.bairro:
        lines.append(hit.bairro)
    if hit.cidade and hit.estado:
        lines.append(f"{hit.cidade} - {hit.estado}")
    if hit.cep:
        lines.append(f"CEP {hit.cep[:5]}-{hit.cep[5:]}" if len(hit.cep) == 8 else f"CEP {hit.cep}")
    if distance_km is not None and distance_km < 9000:
        lines.append(f"Distância: {distance_km:.1f} km")
    return "\n".join(lines)
