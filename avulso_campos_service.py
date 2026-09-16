"""Campos dinâmicos e helpers de Avulso Identificável."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models import AvulsoCampoConfig, AvulsoCampoValor, AvulsoLote, Owner, Saida

CONTEXTOS_AVULSO = {
    "COLETA_AVULSO",
    "ENTRADA_AVULSO",
    "SAIDA_AVULSO",
    "TODOS_AVULSO",
}
TIPOS_CAMPO = {"texto", "telefone", "numero", "foto", "lista"}
ORIGENS_LOTE = {"coleta", "entrada", "saida", "saida_excecao"}
ORIGEM_LABELS = {
    "coleta": "Coleta",
    "entrada": "Entrada",
    "saida": "Saída",
    "saida_excecao": "Cadastro excepcional na saída",
}

STATUS_PENDENTE_SAIDA = {"coletado", "NA_BASE", "na_base"}


def _slug_chave(raw: str) -> str:
    text = unicodedata.normalize("NFD", (raw or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:48] or "campo"


def normalize_contexto_avulso(raw: str) -> str:
    ctx = (raw or "").strip().upper()
    if ctx not in CONTEXTOS_AVULSO:
        raise HTTPException(
            status_code=422,
            detail="contexto inválido. Use COLETA_AVULSO, ENTRADA_AVULSO, SAIDA_AVULSO ou TODOS_AVULSO.",
        )
    return ctx


def normalize_tipo_campo(raw: str) -> str:
    tipo = (raw or "").strip().lower()
    if tipo not in TIPOS_CAMPO:
        raise HTTPException(status_code=422, detail=f"tipo inválido: {raw}")
    return tipo


def parse_opcoes_json(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return []


def resolve_campos_ativos(
    db: Session,
    *,
    sub_base: str,
    contexto: str,
) -> List[AvulsoCampoConfig]:
    ctx = normalize_contexto_avulso(contexto)
    rows = list(
        db.scalars(
            select(AvulsoCampoConfig)
            .where(
                AvulsoCampoConfig.sub_base == sub_base,
                AvulsoCampoConfig.ativo.is_(True),
                AvulsoCampoConfig.contexto.in_([ctx, "TODOS_AVULSO"]),
            )
            .order_by(AvulsoCampoConfig.ordem.asc(), AvulsoCampoConfig.id.asc())
        ).all()
    )
    # Preferência: campo específico do contexto sobrescreve TODOS com mesma chave
    by_chave: Dict[str, AvulsoCampoConfig] = {}
    for row in rows:
        if row.contexto == "TODOS_AVULSO" and row.chave in by_chave:
            continue
        by_chave[row.chave] = row
    ordered = sorted(by_chave.values(), key=lambda r: (int(r.ordem or 0), int(r.id or 0)))
    return ordered


def validate_campos_payload(
    campos_cfg: Sequence[AvulsoCampoConfig],
    valores: Optional[Dict[str, Any]],
    *,
    identificacao_legado: Optional[str] = None,
) -> Dict[str, str]:
    """Valida e normaliza valores. Campos opcionais até existir config ativa."""
    valores = valores or {}
    out: Dict[str, str] = {}
    faltantes: List[str] = []

    # Compat: se não há config e veio identificacao, grava como referência virtual
    if not campos_cfg:
        if identificacao_legado and str(identificacao_legado).strip():
            out["identificacao"] = str(identificacao_legado).strip()[:120]
        return out

    for cfg in campos_cfg:
        raw = valores.get(cfg.chave)
        if raw is None and cfg.chave == "identificacao" and identificacao_legado:
            raw = identificacao_legado
        text = "" if raw is None else str(raw).strip()
        if cfg.obrigatorio and not text:
            faltantes.append(cfg.label or cfg.chave)
            continue
        if not text:
            continue
        if cfg.tipo == "numero":
            try:
                float(text.replace(",", "."))
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Campo '{cfg.label}' deve ser numérico.")
        if cfg.tipo == "lista":
            opcoes = parse_opcoes_json(cfg.opcoes_json)
            if opcoes and text not in opcoes:
                raise HTTPException(status_code=422, detail=f"Valor inválido para '{cfg.label}'.")
        out[cfg.chave] = text[:2000]

    if faltantes:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CAMPOS_AVULSO_OBRIGATORIOS",
                "message": "Preencha os campos obrigatórios do avulso.",
                "faltantes": faltantes,
            },
        )
    return out


def persist_valores_para_saidas(
    db: Session,
    *,
    id_saidas: Sequence[int],
    campos_cfg: Sequence[AvulsoCampoConfig],
    valores_norm: Dict[str, str],
) -> None:
    if not id_saidas or not valores_norm:
        return
    cfg_by_chave = {c.chave: c for c in campos_cfg}
    for id_saida in id_saidas:
        for chave, valor in valores_norm.items():
            cfg = cfg_by_chave.get(chave)
            if not cfg:
                continue
            existing = db.scalar(
                select(AvulsoCampoValor).where(
                    AvulsoCampoValor.id_saida == int(id_saida),
                    AvulsoCampoValor.campo_config_id == int(cfg.id),
                )
            )
            if existing:
                existing.valor_texto = valor
                db.add(existing)
            else:
                db.add(
                    AvulsoCampoValor(
                        id_saida=int(id_saida),
                        campo_config_id=int(cfg.id),
                        valor_texto=valor,
                    )
                )


def create_lote(
    db: Session,
    *,
    sub_base: str,
    origem: str,
    quantidade: int,
    criado_por: Optional[int],
    motivo_excepcional: Optional[str] = None,
) -> AvulsoLote:
    if origem not in ORIGENS_LOTE:
        raise HTTPException(status_code=422, detail=f"origem de lote inválida: {origem}")
    lote = AvulsoLote(
        sub_base=sub_base,
        origem=origem,
        quantidade=int(quantidade),
        criado_por=criado_por,
        motivo_excepcional=(motivo_excepcional or "").strip() or None,
    )
    db.add(lote)
    db.flush()
    return lote


def build_label_amigavel(
    codigo: Optional[str],
    *,
    base_legado: Optional[str] = None,
    campos_cfg: Sequence[AvulsoCampoConfig] = (),
    valores: Optional[Dict[str, str]] = None,
) -> str:
    parts: List[str] = []
    code = (codigo or "").strip()
    if code:
        parts.append(code)
    valores = valores or {}
    id_parts: List[str] = []
    for cfg in campos_cfg:
        if not (cfg.usar_na_identificacao or cfg.exibir_na_selecao):
            continue
        val = (valores.get(cfg.chave) or "").strip()
        if val:
            id_parts.append(val)
    if id_parts:
        parts.append(" • ".join(id_parts[:4]))
    elif base_legado and str(base_legado).strip():
        parts.append(str(base_legado).strip())
    return " ".join(parts) if len(parts) == 1 else (" • ".join(parts) if parts else code or "Avulso")


def origem_amigavel(origem: Optional[str] = None, *, excepcional: bool = False) -> str:
    if excepcional or (origem or "").strip().lower() == "saida_excecao":
        return ORIGEM_LABELS["saida_excecao"]
    key = (origem or "").strip().lower()
    return ORIGEM_LABELS.get(key, "Avulso")


def valores_por_saida(db: Session, id_saida: int) -> Dict[str, str]:
    out: Dict[str, str] = {}
    result = db.execute(
        select(AvulsoCampoConfig.chave, AvulsoCampoValor.valor_texto)
        .join(AvulsoCampoValor, AvulsoCampoValor.campo_config_id == AvulsoCampoConfig.id)
        .where(AvulsoCampoValor.id_saida == int(id_saida))
    ).all()
    for chave, valor in result:
        if chave and valor:
            out[str(chave)] = str(valor)
    return out


def owner_exige_selecao_avulso(db: Session, sub_base: str) -> bool:
    """Coleta e/ou Entrada habilitados → saída deve preferir seleção."""
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        return False
    coleta_on = not bool(getattr(owner, "ignorar_coleta", False))
    entrada_on = bool(getattr(owner, "entrada_obrigatoria_habilitada", False))
    return coleta_on or entrada_on


def list_pendentes(
    db: Session,
    *,
    sub_base: str,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[Saida], int]:
    from sqlalchemy import func

    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    filters = [
        Saida.sub_base == sub_base,
        or_(
            Saida.servico.ilike("%avulso%"),
            Saida.codigo.ilike("AVULSO-%"),
        ),
        Saida.status.in_(["coletado", "NA_BASE", "na_base"]),
    ]
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        ids_from_eav = select(AvulsoCampoValor.id_saida).where(AvulsoCampoValor.valor_texto.ilike(like))
        filters.append(
            or_(
                Saida.codigo.ilike(like),
                Saida.base.ilike(like),
                Saida.id_saida.in_(ids_from_eav),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(Saida).where(*filters)) or 0)
    rows = list(
        db.scalars(
            select(Saida).where(*filters).order_by(Saida.id_saida.desc()).offset(offset).limit(limit)
        ).all()
    )
    return rows, total
