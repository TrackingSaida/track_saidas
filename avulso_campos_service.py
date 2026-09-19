"""Campos dinâmicos e helpers de Avulso Identificável."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from models import AvulsoCampoConfig, AvulsoCampoValor, AvulsoLote, Motoboy, Owner, Saida

CONTEXTOS_META = [
    {
        "id": "TODOS_AVULSO",
        "label": "Todos os fluxos",
        "badges": ["Coleta", "Entrada", "Saída"],
        "hint": "O campo aparece na Coleta, na Entrada e na Saída.",
    },
    {
        "id": "COLETA_AVULSO",
        "label": "Coleta",
        "badges": ["Coleta"],
        "hint": "O campo aparece somente ao lançar avulso na Coleta.",
    },
    {
        "id": "ENTRADA_AVULSO",
        "label": "Entrada",
        "badges": ["Entrada"],
        "hint": "O campo aparece somente ao lançar avulso na Entrada.",
    },
    {
        "id": "SAIDA_AVULSO",
        "label": "Saída",
        "badges": ["Saída"],
        "hint": "O campo aparece somente ao lançar ou selecionar avulso na Saída.",
    },
]
TIPOS_META = [
    {
        "id": "texto",
        "label": "Texto",
        "hint": "Texto livre (nome, bairro, complemento, observação).",
        "placeholder": "Ex.: Maria",
        "input_mode": "text",
    },
    {
        "id": "cep",
        "label": "CEP",
        "hint": "CEP brasileiro com 8 dígitos. Ex.: 01310-100.",
        "placeholder": "00000-000",
        "input_mode": "numeric",
        "mascara": "00000-000",
    },
    {
        "id": "telefone",
        "label": "Telefone",
        "hint": "Telefone com DDD. Ex.: (11) 98888-7777.",
        "placeholder": "(11) 98888-7777",
        "input_mode": "tel",
    },
    {
        "id": "numero",
        "label": "Número",
        "hint": "Valor numérico. Use vírgula ou ponto decimal se precisar.",
        "placeholder": "Ex.: 12",
        "input_mode": "decimal",
    },
    {
        "id": "lista",
        "label": "Lista",
        "hint": "O operador escolhe uma das opções cadastradas.",
        "placeholder": "Selecione",
        "input_mode": "text",
    },
]
TIPOS_LEGADOS = {"primeiro_nome", "segundo_nome"}
# Foto do avulso fica na política do motoboy, não como tipo de campo.
TIPOS_RETIRADOS = {"foto"}
CONTEXTOS_AVULSO = {c["id"] for c in CONTEXTOS_META}
TIPOS_CAMPO = {t["id"] for t in TIPOS_META} | TIPOS_LEGADOS | TIPOS_RETIRADOS
ORIGENS_LOTE = {"coleta", "entrada", "saida", "saida_excecao"}
ORIGEM_LABELS = {
    "coleta": "Coleta",
    "entrada": "Entrada",
    "saida": "Saída",
    "saida_excecao": "Cadastro excepcional na saída",
}

STATUS_PENDENTE_SAIDA = {"coletado", "NA_BASE", "na_base"}
STATUS_HOJE_EXCLUIR = {"entregue", "cancelado", "devolvido", "devolvida"}
_RE_NOME = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' -]{0,79}$")


def contexto_meta(contexto: str) -> Dict[str, Any]:
    ctx = (contexto or "").strip().upper()
    for item in CONTEXTOS_META:
        if item["id"] == ctx:
            return item
    return {"id": ctx, "label": ctx, "badges": [ctx], "hint": ""}


def tipo_meta(tipo: str) -> Dict[str, Any]:
    key = (tipo or "").strip().lower()
    if key in TIPOS_LEGADOS:
        key = "texto"
    for item in TIPOS_META:
        if item["id"] == key:
            return item
    return {
        "id": key,
        "label": (tipo or "Texto").capitalize(),
        "hint": "",
        "placeholder": "",
        "input_mode": "text",
    }


def hoje_operacional() -> date:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    except Exception:
        return date.today()


def status_avulso_label(status: Optional[str]) -> str:
    raw = (status or "").strip()
    key = raw.lower()
    mapping = {
        "coletado": "Coletado",
        "na_base": "Na base",
        "saiu": "Saiu para entrega",
        "saiu_para_entrega": "Saiu para entrega",
        "em_rota": "Em rota",
        "entregue": "Entregue",
        "nao coletado": "Não coletado",
        "não coletado": "Não coletado",
        "ausente": "Ausente",
        "devolvido": "Devolvido",
    }
    if key in mapping:
        return mapping[key]
    if raw:
        return raw.replace("_", " ").strip().capitalize()
    return "Sem status"


def motoboy_nome_saida(db: Session, row: Saida) -> Optional[str]:
    texto = (getattr(row, "entregador", None) or "").strip()
    if texto:
        return texto
    motoboy_id = getattr(row, "motoboy_id", None)
    if not motoboy_id:
        return None
    mb = db.get(Motoboy, int(motoboy_id))
    if not mb:
        return None
    user = getattr(mb, "user", None)
    if user:
        nome = " ".join(p for p in [(user.nome or "").strip(), (user.sobrenome or "").strip()] if p)
        return nome or (user.username or None)
    return None


def _somente_digitos(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def format_valor_tipo(tipo: str, text: str, label: str) -> str:
    tipo_n = (tipo or "texto").strip().lower()
    value = (text or "").strip()
    if tipo_n == "cep":
        digits = _somente_digitos(value)
        if len(digits) != 8:
            raise HTTPException(
                status_code=422,
                detail=f"Campo '{label}' deve ser um CEP com 8 dígitos. Ex.: 01310-100",
            )
        return f"{digits[:5]}-{digits[5:]}"
    if tipo_n == "telefone":
        digits = _somente_digitos(value)
        if digits.startswith("55") and len(digits) in (12, 13):
            digits = digits[2:]
        if len(digits) == 11:
            return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
        if len(digits) == 10:
            return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
        raise HTTPException(
            status_code=422,
            detail=f"Campo '{label}' deve ser um telefone com DDD. Ex.: (11) 98888-7777",
        )
    if tipo_n in ("primeiro_nome", "segundo_nome"):
        cleaned = re.sub(r"\s+", " ", value)
        if not _RE_NOME.match(cleaned):
            exemplo = "Maria" if tipo_n == "primeiro_nome" else "Silva"
            raise HTTPException(
                status_code=422,
                detail=f"Campo '{label}' aceita somente letras. Ex.: {exemplo}",
            )
        return cleaned.title()
    if tipo_n == "numero":
        try:
            float(value.replace(",", "."))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Campo '{label}' deve ser numérico.")
        return value
    return value[:2000]


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
    return [r for r in ordered if str(r.tipo or "").strip().lower() not in TIPOS_RETIRADOS]


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
        text = format_valor_tipo(cfg.tipo, text, cfg.label or cfg.chave)
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


@dataclass
class ListagemAvulsos:
    rows: List[Saida]
    total: int
    modo: str
    ambiguo: bool = False
    mensagem: Optional[str] = None


def _filtro_avulso(sub_base: str):
    return [
        Saida.sub_base == sub_base,
        or_(
            Saida.servico.ilike("%avulso%"),
            Saida.codigo.ilike("AVULSO-%"),
        ),
    ]


def _variantes_busca(tipo: Optional[str], valor: str) -> List[str]:
    raw = (valor or "").strip()
    if not raw:
        return []
    out = [raw]
    if (tipo or "") in ("cep", "telefone"):
        digits = _somente_digitos(raw)
        if digits and digits not in out:
            out.append(digits)
        if tipo == "cep" and len(digits) == 8:
            formatted = f"{digits[:5]}-{digits[5:]}"
            if formatted not in out:
                out.append(formatted)
    return out


def _ids_por_identificadores(
    db: Session,
    *,
    sub_base: str,
    identificadores: Dict[str, str],
) -> Optional[select]:
    matching = None
    for chave, valor in identificadores.items():
        cfg = db.scalar(
            select(AvulsoCampoConfig)
            .where(
                AvulsoCampoConfig.sub_base == sub_base,
                AvulsoCampoConfig.chave == chave,
            )
            .order_by(AvulsoCampoConfig.id.desc())
        )
        tipo = cfg.tipo if cfg else None
        variants = _variantes_busca(tipo, valor)
        if not variants:
            continue
        conds = [AvulsoCampoValor.valor_texto.ilike(f"%{v}%") for v in variants]
        subset = (
            select(AvulsoCampoValor.id_saida)
            .join(AvulsoCampoConfig, AvulsoCampoValor.campo_config_id == AvulsoCampoConfig.id)
            .where(
                AvulsoCampoConfig.sub_base == sub_base,
                AvulsoCampoConfig.chave == chave,
                or_(*conds),
            )
        )
        matching = subset if matching is None else matching.intersect(subset)
    return matching


def _filtros_hoje(sub_base: str) -> List:
    hoje = hoje_operacional()
    return [
        *_filtro_avulso(sub_base),
        Saida.data == hoje,
        or_(
            AvulsoLote.origem.in_(["coleta", "entrada"]),
            and_(
                AvulsoLote.id.is_(None),
                Saida.status.in_(["coletado", "NA_BASE", "na_base"]),
            ),
        ),
        or_(
            Saida.status.is_(None),
            func.lower(func.coalesce(Saida.status, "")).notin_(list(STATUS_HOJE_EXCLUIR)),
        ),
    ]


def _aplicar_busca_contem(
    filters: List,
    *,
    sub_base: str,
    term: str,
    ids: Dict[str, str],
    db: Session,
) -> Optional[List]:
    out = list(filters)
    if ids:
        matching = _ids_por_identificadores(db, sub_base=sub_base, identificadores=ids)
        if matching is None:
            return None
        out.append(Saida.id_saida.in_(matching))
    if term:
        like = f"%{term}%"
        ids_from_eav = select(AvulsoCampoValor.id_saida).where(AvulsoCampoValor.valor_texto.ilike(like))
        out.append(
            or_(
                Saida.codigo.ilike(like),
                Saida.base.ilike(like),
                Saida.id_saida.in_(ids_from_eav),
            )
        )
    return out


def _contar_e_listar(db: Session, filters: List, *, joined_lote: bool, limit: int, offset: int) -> tuple:
    stmt = select(Saida)
    if joined_lote:
        stmt = stmt.outerjoin(AvulsoLote, AvulsoLote.id == Saida.avulso_lote_id)
    stmt = stmt.where(*filters)
    total = int(
        db.scalar(
            select(func.count()).select_from(
                stmt.with_only_columns(Saida.id_saida).order_by(None).subquery()
            )
        )
        or 0
    )
    rows = list(
        db.scalars(stmt.order_by(Saida.id_saida.desc()).offset(offset).limit(limit)).all()
    )
    return rows, total


def list_pendentes(
    db: Session,
    *,
    sub_base: str,
    q: Optional[str] = None,
    identificadores: Optional[Dict[str, str]] = None,
    todos_do_dia: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> ListagemAvulsos:
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    ids = {
        str(k).strip(): str(v).strip()
        for k, v in (identificadores or {}).items()
        if str(k).strip() and str(v).strip()
    }
    term = (q or "").strip()
    busca = bool(ids or term)

    if not busca and not todos_do_dia:
        return ListagemAvulsos(
            rows=[],
            total=0,
            modo="idle",
            mensagem="Digite para buscar os avulsos de hoje.",
        )

    if busca:
        filtros_hoje = _aplicar_busca_contem(
            _filtros_hoje(sub_base),
            sub_base=sub_base,
            term=term,
            ids=ids,
            db=db,
        )
        if filtros_hoje is None:
            return ListagemAvulsos(
                rows=[],
                total=0,
                modo="busca",
                mensagem="Nenhum avulso de hoje com esses dados.",
            )
        rows, total = _contar_e_listar(
            db, filtros_hoje, joined_lote=True, limit=limit, offset=offset
        )
        if total > 0:
            return ListagemAvulsos(rows=rows, total=total, modo="busca")

        filtros_outros = _aplicar_busca_contem(
            _filtro_avulso(sub_base),
            sub_base=sub_base,
            term=term,
            ids=ids,
            db=db,
        )
        if filtros_outros is None:
            return ListagemAvulsos(
                rows=[],
                total=0,
                modo="busca",
                mensagem="Nenhum avulso encontrado com esses dados.",
            )
        outros_rows, outros_total = _contar_e_listar(
            db, filtros_outros, joined_lote=False, limit=2, offset=0
        )
        if outros_total == 0:
            return ListagemAvulsos(
                rows=[],
                total=0,
                modo="busca",
                mensagem="Nenhum avulso encontrado com esses dados.",
            )
        if outros_total > 1:
            return ListagemAvulsos(
                rows=[],
                total=outros_total,
                modo="busca",
                ambiguo=True,
                mensagem="Nada encontrado hoje. Em outros dias há vários avulsos com esses dados. Refine a busca ou leia a etiqueta.",
            )
        return ListagemAvulsos(rows=outros_rows[:1], total=1, modo="busca")

    rows, total = _contar_e_listar(
        db, _filtros_hoje(sub_base), joined_lote=True, limit=limit, offset=offset
    )
    return ListagemAvulsos(rows=rows, total=total, modo="hoje")
