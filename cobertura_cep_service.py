"""Área de cobertura por prefixo de CEP (sub_base), com regiões nomeadas opcionais."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CoberturaCepPrefixo, CoberturaRegiao

_DIGITS_RE = re.compile(r"\D+")


def normalize_cep_digits(cep: Optional[str]) -> str:
    return _DIGITS_RE.sub("", cep or "")


def normalize_prefixo(prefixo: Optional[str]) -> str:
    return normalize_cep_digits(prefixo)


def list_prefixos_ativos(db: Session, sub_base: str) -> List[str]:
    sub = (sub_base or "").strip()
    if not sub:
        return []
    rows = db.scalars(
        select(CoberturaCepPrefixo.prefixo)
        .where(
            CoberturaCepPrefixo.sub_base == sub,
            CoberturaCepPrefixo.ativo.is_(True),
        )
        .order_by(CoberturaCepPrefixo.prefixo)
    ).all()
    return [normalize_prefixo(p) for p in rows if normalize_prefixo(p)]


def listar_cobertura_estruturada(db: Session, sub_base: str) -> Dict[str, Any]:
    """Retorna modo + regiões (nome + prefixos) + prefixos sem região."""
    sub = (sub_base or "").strip()
    prefixos = list(
        db.scalars(
            select(CoberturaCepPrefixo).where(
                CoberturaCepPrefixo.sub_base == sub,
                CoberturaCepPrefixo.ativo.is_(True),
            )
        ).all()
    )
    regioes_rows = list(
        db.scalars(
            select(CoberturaRegiao)
            .where(CoberturaRegiao.sub_base == sub, CoberturaRegiao.ativo.is_(True))
            .order_by(CoberturaRegiao.ordem.asc(), CoberturaRegiao.nome.asc())
        ).all()
    )
    by_reg: Dict[int, List[str]] = {}
    soltos: List[str] = []
    for row in prefixos:
        p = normalize_prefixo(row.prefixo)
        if not p:
            continue
        rid = getattr(row, "id_regiao", None)
        if rid:
            by_reg.setdefault(int(rid), []).append(p)
        else:
            soltos.append(p)

    regioes_out: List[Dict[str, Any]] = []
    for reg in regioes_rows:
        prefs = sorted(set(by_reg.get(int(reg.id), [])))
        regioes_out.append(
            {
                "id": int(reg.id),
                "nome": (reg.nome or "").strip(),
                "prefixos": prefs,
            }
        )
    # prefixos órfãos apontando para região inexistente
    known = {int(r["id"]) for r in regioes_out}
    for rid, prefs in by_reg.items():
        if rid not in known:
            soltos.extend(prefs)

    soltos = sorted(set(normalize_prefixo(p) for p in soltos if normalize_prefixo(p)))
    if not regioes_out and not soltos:
        modo = "ilimitado"
    elif regioes_out:
        modo = "regioes"
    else:
        modo = "prefixos"
    return {"modo": modo, "regioes": regioes_out, "prefixos_sem_regiao": soltos}


def replace_prefixos(db: Session, sub_base: str, prefixos: Sequence[str]) -> List[str]:
    """Compat: substitui todos os prefixos do sub_base (sem região)."""
    sub = (sub_base or "").strip()
    cleaned: List[str] = []
    seen = set()
    for raw in prefixos or []:
        p = normalize_prefixo(raw)
        if not p or p in seen:
            continue
        if len(p) < 2:
            continue
        seen.add(p)
        cleaned.append(p)

    existing = list(
        db.scalars(select(CoberturaCepPrefixo).where(CoberturaCepPrefixo.sub_base == sub)).all()
    )
    by_prefix = {normalize_prefixo(r.prefixo): r for r in existing}
    keep = set(cleaned)
    for p, row in by_prefix.items():
        if p not in keep:
            db.delete(row)
    for p in cleaned:
        row = by_prefix.get(p)
        if row:
            row.ativo = True
            row.prefixo = p
            row.id_regiao = None
        else:
            db.add(CoberturaCepPrefixo(sub_base=sub, prefixo=p, ativo=True, id_regiao=None))
    return cleaned


def replace_regioes(
    db: Session,
    sub_base: str,
    regioes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Substitui o mapa de cobertura por regiões nomeadas.
    Cada item: { nome: str, prefixos: [str] }.
    """
    sub = (sub_base or "").strip()
    # limpa prefixos e regiões atuais
    for row in list(
        db.scalars(select(CoberturaCepPrefixo).where(CoberturaCepPrefixo.sub_base == sub)).all()
    ):
        db.delete(row)
    for row in list(
        db.scalars(select(CoberturaRegiao).where(CoberturaRegiao.sub_base == sub)).all()
    ):
        db.delete(row)
    db.flush()

    used_prefixes = set()
    out_regioes: List[Dict[str, Any]] = []
    for idx, item in enumerate(regioes or []):
        if not isinstance(item, dict):
            continue
        nome = str(item.get("nome") or "").strip()
        if not nome:
            continue
        prefs_raw = item.get("prefixos") or []
        prefs: List[str] = []
        for raw in prefs_raw:
            p = normalize_prefixo(raw)
            if not p or len(p) < 2 or p in used_prefixes:
                continue
            used_prefixes.add(p)
            prefs.append(p)
        reg = CoberturaRegiao(sub_base=sub, nome=nome[:120], ativo=True, ordem=idx)
        db.add(reg)
        db.flush()
        for p in prefs:
            db.add(
                CoberturaCepPrefixo(
                    sub_base=sub,
                    prefixo=p,
                    ativo=True,
                    id_regiao=int(reg.id),
                )
            )
        out_regioes.append({"id": int(reg.id), "nome": nome, "prefixos": prefs})
    return listar_cobertura_estruturada(db, sub)


def cep_coberto(cep: Optional[str], prefixos: Sequence[str]) -> bool:
    if not prefixos:
        return True
    digits = normalize_cep_digits(cep)
    if not digits:
        return False
    return any(digits.startswith(p) for p in prefixos if p)


def avaliar_cobertura(
    db: Session, sub_base: str, cep: Optional[str]
) -> Tuple[bool, List[str]]:
    prefixos = list_prefixos_ativos(db, sub_base)
    return cep_coberto(cep, prefixos), prefixos


def avaliar_cobertura_detalhada(
    db: Session, sub_base: str, cep: Optional[str]
) -> Dict[str, Any]:
    """Inclui região casada e estrutura completa."""
    estrutura = listar_cobertura_estruturada(db, sub_base)
    digits = normalize_cep_digits(cep)
    prefixos = list_prefixos_ativos(db, sub_base)
    ok = cep_coberto(cep, prefixos)
    regiao_nome = None
    prefixo_match = None
    if ok and digits and prefixos:
        for reg in estrutura.get("regioes") or []:
            for p in reg.get("prefixos") or []:
                if digits.startswith(p):
                    regiao_nome = reg.get("nome")
                    prefixo_match = p
                    break
            if regiao_nome:
                break
        if not prefixo_match:
            for p in estrutura.get("prefixos_sem_regiao") or []:
                if digits.startswith(p):
                    prefixo_match = p
                    break
    return {
        "coberto": ok,
        "cep": digits,
        "regiao_nome": regiao_nome,
        "prefixo_match": prefixo_match,
        **estrutura,
    }
