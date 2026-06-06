"""Serviço de endereços conhecidos (histórico operacional)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from address_fuzzy import (
    FUZZY_DID_YOU_MEAN_THRESHOLD,
    FUZZY_LOW_SCORE_THRESHOLD,
    extract_query_street,
    find_did_you_mean,
    similarity,
)
from address_normalizer import (
    normalize_address_key,
    normalize_cep,
    normalize_estado_uf,
    normalize_street_part,
    normalizeAddressQuery,
)
from address_providers.base import RawAddressHit
from models import EnderecoConhecido

logger = logging.getLogger(__name__)

FUZZY_CANDIDATES_LIMIT = int(os.getenv("FUZZY_CANDIDATES_LIMIT", "200"))


def _row_to_hit(row: EnderecoConhecido) -> RawAddressHit:
    return RawAddressHit(
        rua=row.rua or "",
        numero=str(row.numero or ""),
        bairro=row.bairro or "",
        cidade=row.cidade or "",
        estado=normalize_estado_uf(row.estado),
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
        q_street = extract_query_street(query)
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
                    estado=normalize_estado_uf(estado),
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


def _saida_detail_candidates(db: Session, sub_base: str, limit: int) -> List[Tuple[str, str, str]]:
    try:
        from models import Saida, SaidaDetail

        rows = db.execute(
            select(
                SaidaDetail.dest_rua,
                SaidaDetail.dest_cidade,
                SaidaDetail.dest_estado,
                func.count(),
            )
            .join(Saida, Saida.id_saida == SaidaDetail.id_saida)
            .where(
                Saida.sub_base == sub_base,
                SaidaDetail.dest_rua.isnot(None),
                SaidaDetail.dest_rua != "",
            )
            .group_by(SaidaDetail.dest_rua, SaidaDetail.dest_cidade, SaidaDetail.dest_estado)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
        return [(r[0], r[1] or "", r[2] or "") for r in rows if r[0]]
    except Exception as e:
        logger.debug("saida_detail fuzzy candidates skip: %s", e)
        return []


def get_fuzzy_candidates(db: Session, sub_base: str, limit: int = FUZZY_CANDIDATES_LIMIT) -> List[Tuple[str, str, str]]:
    seen: set[Tuple[str, str, str]] = set()
    candidates: List[Tuple[str, str, str]] = []

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
        for rua, cidade, estado in rows:
            if not rua:
                continue
            key = (rua, cidade or "", estado or "")
            if key not in seen:
                seen.add(key)
                candidates.append(key)
    except Exception:
        pass

    for item in _saida_detail_candidates(db, sub_base, limit):
        if item not in seen:
            seen.add(item)
            candidates.append(item)

    return candidates[:limit]


def _resolve_known_row(
    db: Session,
    sub_base: str,
    rua: str,
    cidade: str,
    street_q: str,
    threshold: float,
) -> Optional[EnderecoConhecido]:
    try:
        q = (
            select(EnderecoConhecido)
            .where(EnderecoConhecido.sub_base == sub_base)
            .order_by(EnderecoConhecido.qtd_utilizacoes.desc())
            .limit(30)
        )
        if cidade:
            q = q.where(EnderecoConhecido.cidade.ilike(cidade))
        rows = db.execute(q).scalars().all()
        for row in rows:
            if similarity(street_q, row.rua or "") >= threshold:
                return row
        for row in rows:
            if normalize_street_part(rua) in normalize_street_part(row.rua or ""):
                return row
    except Exception:
        pass
    return None


def build_did_you_mean(
    db: Session,
    sub_base: str,
    query: str,
    hints: Optional[dict] = None,
    known_hits: Optional[List[Tuple[RawAddressHit, int]]] = None,
) -> Optional[dict]:
    street_q = extract_query_street(query, hints)
    threshold = FUZZY_DID_YOU_MEAN_THRESHOLD

    candidates = get_fuzzy_candidates(db, sub_base)
    match = find_did_you_mean(query, candidates, threshold=threshold, hints=hints)
    if match:
        rua, cidade, estado, _sim = match
        row = _resolve_known_row(db, sub_base, rua, cidade, street_q, threshold)
        if row:
            hit = _row_to_hit(row)
            return {
                "original_query": query,
                "suggestion": _format_suggestion_dict(hit, 75, 0.65, int(row.qtd_utilizacoes or 1)),
            }

    if known_hits:
        best: Optional[Tuple[RawAddressHit, int, float]] = None
        for hit, qtd in known_hits:
            if not hit.latitude or not hit.longitude or not hit.rua:
                continue
            sim = similarity(street_q, hit.rua)
            if sim >= 0.5 and (best is None or sim > best[2]):
                best = (hit, qtd, sim)
        if best:
            hit, qtd, _sim = best
            return {
                "original_query": query,
                "suggestion": _format_suggestion_dict(hit, 70, 0.6, qtd),
            }

    return None


def build_did_you_mean_from_below_threshold(
    below: List[Tuple[RawAddressHit, int, int, float, float]],
    search_query: str,
    hints: Optional[dict] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Optional[dict]:
    street_q = extract_query_street(search_query, hints)
    best: Optional[Tuple[RawAddressHit, int, int, float, float, float]] = None

    for hit, known_qtd, score, confidence, dist in below:
        if not hit.rua or not hit.cidade:
            continue
        sim = similarity(street_q, hit.rua)
        if sim < FUZZY_LOW_SCORE_THRESHOLD:
            continue
        if latitude is not None and longitude is not None and dist > 60:
            continue
        if best is None or sim > best[5] or (sim == best[5] and score > best[2]):
            best = (hit, known_qtd, score, confidence, dist, sim)

    if not best:
        return None
    hit, known_qtd, score, confidence, _dist, _sim = best
    return build_did_you_mean_from_hit(
        search_query,
        hit,
        max(score, 15),
        min(confidence, 0.65),
        known_qtd,
    )


def build_did_you_mean_from_hit(
    query: str,
    hit: RawAddressHit,
    score: int,
    confidence: float,
    known_qtd: int = 0,
) -> dict:
    return {
        "original_query": query,
        "suggestion": _format_suggestion_dict(hit, score, confidence, known_qtd),
    }


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
