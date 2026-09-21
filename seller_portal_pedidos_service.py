"""Acompanhamento de pedidos no portal do seller (tenant-safe)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from codigo_normalizer import canonicalize_servico
from models import BasePreco, EnvioProprio, Motoboy, Saida, SaidaDetail
from motoboy_nome_utils import get_motoboy_display_name
from saida_historico_service import listar_historico_saida

STATUS_ETIQUETADO = "ETIQUETADO"

CANAL_SITE = "site"
CANAL_ML = "mercado_livre"
CANAL_SHOPEE = "shopee"

CANAL_LABELS = {
    CANAL_SITE: "Site",
    CANAL_ML: "Mercado Livre",
    CANAL_SHOPEE: "Shopee",
}

# Eventos internos da operação — não interessam ao seller.
_EVENTOS_OCULTOS = {
    "lido",
    "scan",
    "assumir",
    "assumido",
    "reatribuicao",
    "reatribuido",
    "reatribuido_em_rota",
    "nova_saida_mesmo_entregador",
    "removido_sem_inicio",
    "desatribuido",
    "saida_conferida",
    "saida_reconferida",
    "entrada_base",
    "status_coletado_manual",
    "status_nao_coletado_manual",
}

_ROTULOS_SELLER = {
    "etiqueta_gerada": "Etiqueta emitida",
    "etiqueta_cancelada": "Etiqueta cancelada",
    "criado_coleta": "Pacote coletado",
    "coleta": "Pacote coletado",
    "lancar_avulso": "Pacote coletado",
    "em_rota": "Saiu para entrega",
    "status_saiu_manual": "Saiu para entrega",
    "ausente": "Destinatário ausente",
    "ausente_lote": "Destinatário ausente",
    "nova_tentativa": "Nova tentativa de entrega",
    "liberacao_ausencias": "Nova tentativa de entrega",
    "entregue": "Entregue",
    "entregue_lote": "Entregue",
    "cancelado": "Pedido cancelado",
    "devolucao": "Devolvido",
    "encerrado_sistema": "Encerrado",
    "rota_cancelada": "Rota cancelada",
}

_TIPO_EVENTO = {
    "etiqueta_gerada": "emitida",
    "etiqueta_cancelada": "cancelado",
    "criado_coleta": "coletado",
    "coleta": "coletado",
    "lancar_avulso": "coletado",
    "em_rota": "em_entrega",
    "status_saiu_manual": "em_entrega",
    "ausente": "ausente",
    "ausente_lote": "ausente",
    "nova_tentativa": "tentativa",
    "liberacao_ausencias": "tentativa",
    "entregue": "entregue",
    "entregue_lote": "entregue",
    "cancelado": "cancelado",
    "devolucao": "devolvido",
    "encerrado_sistema": "encerrado",
    "rota_cancelada": "cancelado",
}

# Eventos em que o seller pode ver o entregador atribuído.
_EVENTOS_COM_ENTREGADOR = {
    "em_rota",
    "status_saiu_manual",
    "ausente",
    "ausente_lote",
    "nova_tentativa",
    "liberacao_ausencias",
    "entregue",
    "entregue_lote",
}

_STATUS_FILTRO = {
    "aguardando_coleta": ("ETIQUETADO",),
    "coletado": ("COLETADO", "SAIU"),
    "em_entrega": (
        "SAIU_PARA_ENTREGA",
        "EM_ROTA",
        "SAIU PRA ENTREGA",
        "SAIU_PRA_ENTREGA",
        "SAIU_PARA_ENTREGA",
    ),
    "ausente": ("AUSENTE",),
    "entregue": ("ENTREGUE",),
    "cancelado": ("CANCELADO",),
    "devolvido": ("DEVOLVIDO", "DEVOLUCAO"),
}


def canal_venda_chave(servico: Optional[str]) -> str:
    label = canonicalize_servico(servico)
    if label == "Shopee":
        return CANAL_SHOPEE
    if label == "Mercado Livre":
        return CANAL_ML
    return CANAL_SITE


def canal_venda_label(servico: Optional[str]) -> str:
    return CANAL_LABELS[canal_venda_chave(servico)]


def status_pedido_amigavel(status: Optional[str]) -> str:
    st = (status or "").strip().upper().replace("-", "_")
    st_compact = st.replace(" ", "_")
    if st_compact == "ETIQUETADO" or not st:
        return "Aguardando coleta"
    if st_compact == "CANCELADO":
        return "Cancelado"
    if st_compact in ("AUSENTE",):
        return "Destinatário ausente"
    if st_compact in ("ENTREGUE",):
        return "Entregue"
    if st_compact in ("DEVOLVIDO", "DEVOLUCAO"):
        return "Devolvido"
    if st_compact in ("ENCERRADO_SISTEMA", "ENCERRADO"):
        return "Encerrado"
    if st_compact in (
        "SAIU_PARA_ENTREGA",
        "EM_ROTA",
        "SAIU_PRA_ENTREGA",
        "SAIU_PRA_ENTREGA",
    ) or "SAIU_PARA" in st_compact or st_compact == "EM_ROTA":
        return "Saiu para entrega"
    if st_compact in ("COLETADO", "SAIU"):
        return "Coletado"
    return "Coletado"


def rotulo_timeline_seller(evento: Optional[str]) -> Optional[str]:
    key = (evento or "").strip().lower()
    if not key or key in _EVENTOS_OCULTOS:
        return None
    return _ROTULOS_SELLER.get(key)


def tipo_timeline_seller(evento: Optional[str]) -> str:
    key = (evento or "").strip().lower()
    return _TIPO_EVENTO.get(key, "outro")


def _escape_like(raw: str) -> str:
    return "".join(ch for ch in raw if ch not in "%_\\")


def _nome_base_seller(db: Session, seller) -> str:
    base = db.get(BasePreco, int(seller.id_base))
    if not base or (base.sub_base or "").strip() != seller.sub_base:
        raise HTTPException(404, "Seller não encontrado.")
    return (base.base or "").strip()


def saida_visivel_ao_seller(db: Session, seller, saida: Saida) -> bool:
    """True se o pacote é do seller: envio próprio dele, ou coleta na loja dele sem etiqueta de outro."""
    if saida is None:
        return False
    if (saida.sub_base or "").strip() != seller.sub_base:
        return False
    envio = db.scalar(select(EnvioProprio).where(EnvioProprio.id_saida == saida.id_saida).limit(1))
    if envio is not None:
        return (envio.sub_base or "").strip() == seller.sub_base and int(envio.id_base or 0) == int(
            seller.id_base
        )
    nome = _nome_base_seller(db, seller)
    return bool(nome) and (saida.base or "").strip() == nome


def filtro_pedidos_seller(seller, nome_base: str):
    """Escopo SQL: envio_proprio do seller OU saida.base da loja sem envio_proprio alheio."""
    proprio = and_(
        EnvioProprio.id_base == seller.id_base,
        EnvioProprio.sub_base == seller.sub_base,
    )
    if not (nome_base or "").strip():
        return and_(Saida.sub_base == seller.sub_base, proprio)
    return and_(
        Saida.sub_base == seller.sub_base,
        or_(
            proprio,
            and_(EnvioProprio.id_envio.is_(None), Saida.base == nome_base),
        ),
    )


def _filtro_canal(canal: Optional[str]):
    chave = (canal or "").strip().lower().replace(" ", "_")
    if chave in ("avulso",):
        chave = CANAL_SITE
    if chave in ("ml",):
        chave = CANAL_ML
    if not chave:
        return None
    servico = func.lower(func.coalesce(Saida.servico, ""))
    if chave == CANAL_SHOPEE:
        return servico.contains("shopee")
    if chave == CANAL_ML:
        return or_(servico.contains("mercado"), servico.contains("flex"), servico == "ml")
    if chave == CANAL_SITE:
        return and_(
            ~servico.contains("shopee"),
            ~servico.contains("mercado"),
            ~servico.contains("flex"),
        )
    return None


def _filtro_status(status: Optional[str]):
    chave = (status or "").strip().lower()
    valores = _STATUS_FILTRO.get(chave)
    if not valores:
        return None
    return func.upper(func.coalesce(Saida.status, "")).in_(valores)


def _filtro_busca(q: Optional[str]):
    term = (q or "").strip()[:80]
    if not term:
        return None
    like = f"%{_escape_like(term)}%"
    dest_detail = exists(
        select(SaidaDetail.id_detail).where(
            SaidaDetail.id_saida == Saida.id_saida,
            SaidaDetail.dest_nome.ilike(like),
        )
    )
    dest_envio = and_(EnvioProprio.id_envio.isnot(None), EnvioProprio.dest_nome.ilike(like))
    return or_(Saida.codigo.ilike(like), dest_detail, dest_envio)


def _parse_date_bound(raw: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    txt = (raw or "").strip()[:10]
    if not txt:
        return None
    try:
        d = datetime.strptime(txt, "%Y-%m-%d").date()
    except ValueError:
        return None
    if end_of_day:
        return datetime.combine(d, datetime.max.time().replace(microsecond=0))
    return datetime.combine(d, datetime.min.time())


def _filtro_periodo(de: Optional[str], ate: Optional[str]):
    start = _parse_date_bound(de, end_of_day=False)
    end = _parse_date_bound(ate, end_of_day=True)
    conds = []
    if start is not None:
        conds.append(Saida.timestamp >= start)
    if end is not None:
        conds.append(Saida.timestamp <= end)
    if not conds:
        return None
    return and_(*conds)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _dest_from_envio(envio: Optional[EnvioProprio]) -> Dict[str, Any]:
    if not envio:
        return {}
    return {
        "nome": envio.dest_nome,
        "telefone": envio.dest_telefone,
        "cep": envio.dest_cep,
        "rua": envio.dest_rua,
        "numero": envio.dest_numero,
        "complemento": envio.dest_complemento,
        "bairro": envio.dest_bairro,
        "cidade": envio.dest_cidade,
        "uf": envio.dest_uf,
    }


def _dest_from_detail(detail: Optional[SaidaDetail]) -> Dict[str, Any]:
    if not detail:
        return {}
    return {
        "nome": detail.dest_nome,
        "telefone": detail.dest_contato,
        "cep": detail.dest_cep,
        "rua": detail.dest_rua,
        "numero": detail.dest_numero,
        "complemento": detail.dest_complemento,
        "bairro": detail.dest_bairro,
        "cidade": detail.dest_cidade,
        "uf": detail.dest_estado,
    }


def _merge_dest(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    keys = ("nome", "telefone", "cep", "rua", "numero", "complemento", "bairro", "cidade", "uf")
    for k in keys:
        val = (primary.get(k) or "").strip() if primary.get(k) is not None else ""
        if not val:
            fb = fallback.get(k)
            val = (fb or "").strip() if fb is not None else ""
        out[k] = val or None
    return out


def _endereco_linha(dest: Dict[str, Any]) -> Optional[str]:
    partes = [
        dest.get("rua"),
        dest.get("numero"),
        dest.get("complemento"),
        dest.get("bairro"),
        dest.get("cidade"),
        dest.get("uf"),
        dest.get("cep"),
    ]
    txt = ", ".join(p for p in partes if p)
    return txt or None


def _load_details_map(db: Session, saida_ids: List[int]) -> Dict[int, SaidaDetail]:
    if not saida_ids:
        return {}
    subq = (
        select(SaidaDetail.id_saida, func.max(SaidaDetail.id_detail).label("id_detail"))
        .where(SaidaDetail.id_saida.in_(saida_ids))
        .group_by(SaidaDetail.id_saida)
        .subquery()
    )
    rows = db.scalars(select(SaidaDetail).join(subq, SaidaDetail.id_detail == subq.c.id_detail)).all()
    return {int(r.id_saida): r for r in rows}


def _load_envios_map(db: Session, saida_ids: List[int]) -> Dict[int, EnvioProprio]:
    if not saida_ids:
        return {}
    rows = db.scalars(select(EnvioProprio).where(EnvioProprio.id_saida.in_(saida_ids))).all()
    return {int(r.id_saida): r for r in rows if r.id_saida}


def _codigo_marketplace(saida: Saida) -> Optional[str]:
    if getattr(saida, "ml_order_id", None):
        return str(saida.ml_order_id)
    return None


def _item_resumo(
    saida: Saida,
    envio: Optional[EnvioProprio],
    detail: Optional[SaidaDetail],
) -> Dict[str, Any]:
    dest = _merge_dest(_dest_from_detail(detail), _dest_from_envio(envio))
    st = getattr(saida, "status", None)
    return {
        "id_saida": int(saida.id_saida),
        "id_envio": int(envio.id_envio) if envio else None,
        "codigo": saida.codigo,
        "codigo_marketplace": _codigo_marketplace(saida),
        "canal": canal_venda_chave(saida.servico),
        "canal_label": canal_venda_label(saida.servico),
        "status": st,
        "status_label": status_pedido_amigavel(st),
        "dest_nome": dest.get("nome"),
        "dest_cidade": dest.get("cidade"),
        "dest_uf": dest.get("uf"),
        "created_at": _iso(getattr(saida, "timestamp", None)),
        "pode_cancelar": (st or "").strip().upper() == STATUS_ETIQUETADO and envio is not None,
    }


def _nome_motoboy_por_id(db: Session, motoboy_id: Optional[int]) -> Optional[str]:
    if not motoboy_id:
        return None
    try:
        mid = int(motoboy_id)
    except (TypeError, ValueError):
        return None
    motoboy = db.get(Motoboy, mid)
    if not motoboy:
        return None
    nome = (get_motoboy_display_name(db, motoboy=motoboy) or "").strip()
    return nome or None


def _nome_entregador_saida(db: Session, saida: Optional[Saida]) -> Optional[str]:
    if saida is None:
        return None
    nome = (getattr(saida, "entregador", None) or "").strip()
    if nome:
        return nome
    return _nome_motoboy_por_id(db, getattr(saida, "motoboy_id", None))


def _detalhe_recebimento(detail: Optional[SaidaDetail]) -> Optional[str]:
    if detail is None:
        return None
    nome = (getattr(detail, "nome_recebedor", None) or "").strip()
    if not nome:
        return None
    tipo = (getattr(detail, "tipo_recebedor", None) or "").strip()
    if tipo:
        return f"Recebido por {nome} ({tipo})"
    return f"Recebido por {nome}"


def _join_detalhe(*parts: Optional[str]) -> Optional[str]:
    cleaned = [p.strip() for p in parts if p and str(p).strip()]
    return " · ".join(cleaned) if cleaned else None


def projetar_timeline_seller(
    db: Session,
    id_saida: int,
    saida: Optional[Saida] = None,
    detail: Optional[SaidaDetail] = None,
) -> List[Dict[str, Any]]:
    """Timeline amigável ao seller: mais recente no topo; inclui status atual e entregador."""
    items = listar_historico_saida(db, id_saida)
    out: List[Dict[str, Any]] = []
    for item in items:
        key = (item.evento or "").strip().lower()
        rotulo = rotulo_timeline_seller(item.evento)
        if not rotulo:
            continue
        extra = None
        if item.motivo_ocorrencia:
            extra = item.motivo_ocorrencia
            if item.tentativa:
                extra = f"Tentativa {item.tentativa}: {extra}"
        elif item.tentativa:
            extra = f"Tentativa {item.tentativa}"

        entregador = None
        if key in _EVENTOS_COM_ENTREGADOR:
            entregador = _nome_motoboy_por_id(db, getattr(item, "motoboy_id_novo", None))
            if not entregador:
                entregador = _nome_entregador_saida(db, saida)
            if entregador:
                entregador = f"Entregador: {entregador}"

        recebimento = None
        if key in ("entregue", "entregue_lote"):
            recebimento = _detalhe_recebimento(detail)

        out.append(
            {
                "quando": _iso(item.timestamp),
                "titulo": rotulo,
                "tipo": tipo_timeline_seller(item.evento),
                "detalhe": _join_detalhe(extra, entregador, recebimento),
                "motoboy_nome": (entregador or "").replace("Entregador: ", "") or None,
            }
        )

    # Garante que o status atual apareça na timeline (ex.: saiu sem evento amigável).
    if saida is not None:
        label_atual = status_pedido_amigavel(getattr(saida, "status", None))
        last_title = out[-1]["titulo"] if out else None
        if label_atual and label_atual != last_title:
            quando = None
            if detail is not None and getattr(detail, "timestamp", None):
                quando = _iso(detail.timestamp)
            if not quando and getattr(saida, "data_hora_entrega", None):
                quando = _iso(saida.data_hora_entrega)
            if not quando:
                quando = _iso(getattr(saida, "timestamp", None))
            entregador = _nome_entregador_saida(db, saida)
            recebimento = None
            st = (getattr(saida, "status", None) or "").strip().upper().replace(" ", "_")
            if st == "ENTREGUE":
                recebimento = _detalhe_recebimento(detail)
            out.append(
                {
                    "quando": quando,
                    "titulo": label_atual,
                    "tipo": "status_atual",
                    "detalhe": _join_detalhe(
                        f"Entregador: {entregador}" if entregador else None,
                        recebimento,
                    ),
                    "motoboy_nome": entregador,
                }
            )
        elif out and out[-1].get("tipo") in ("em_entrega", "entregue", "ausente", "status_atual"):
            # Enriquecer último evento com entregador atual se ainda não tiver.
            if not out[-1].get("motoboy_nome"):
                entregador = _nome_entregador_saida(db, saida)
                if entregador:
                    out[-1]["motoboy_nome"] = entregador
                    out[-1]["detalhe"] = _join_detalhe(out[-1].get("detalhe"), f"Entregador: {entregador}")

    if not out and saida is not None:
        out.append(
            {
                "quando": _iso(getattr(saida, "timestamp", None)),
                "titulo": status_pedido_amigavel(getattr(saida, "status", None)),
                "tipo": "outro",
                "detalhe": None,
                "motoboy_nome": _nome_entregador_saida(db, saida),
            }
        )

    # Padrão de mercado (Correios, marketplaces): última atualização no topo.
    out.reverse()
    return out


def listar_pedidos_seller(
    db: Session,
    seller,
    *,
    page: int = 1,
    per_page: int = 20,
    q: Optional[str] = None,
    canal: Optional[str] = None,
    status: Optional[str] = None,
    de: Optional[str] = None,
    ate: Optional[str] = None,
) -> Dict[str, Any]:
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 20)))
    nome_base = _nome_base_seller(db, seller)
    conds = [filtro_pedidos_seller(seller, nome_base)]
    fc = _filtro_canal(canal)
    if fc is not None:
        conds.append(fc)
    fs = _filtro_status(status)
    if fs is not None:
        conds.append(fs)
    fq = _filtro_busca(q)
    if fq is not None:
        conds.append(fq)
    fp = _filtro_periodo(de, ate)
    if fp is not None:
        conds.append(fp)

    joined = (
        select(Saida)
        .outerjoin(EnvioProprio, EnvioProprio.id_saida == Saida.id_saida)
        .where(*conds)
    )
    total = int(
        db.scalar(
            select(func.count(Saida.id_saida))
            .select_from(Saida)
            .outerjoin(EnvioProprio, EnvioProprio.id_saida == Saida.id_saida)
            .where(*conds)
        )
        or 0
    )
    rows = list(
        db.scalars(
            joined.order_by(Saida.timestamp.desc(), Saida.id_saida.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    ids = [int(r.id_saida) for r in rows]
    envios = _load_envios_map(db, ids)
    details = _load_details_map(db, ids)
    items = [_item_resumo(s, envios.get(int(s.id_saida)), details.get(int(s.id_saida))) for s in rows]
    return {"total": total, "page": page, "per_page": per_page, "items": items}


def detalhe_pedido_seller(db: Session, seller, id_saida: int) -> Dict[str, Any]:
    saida = db.get(Saida, int(id_saida))
    if not saida or not saida_visivel_ao_seller(db, seller, saida):
        raise HTTPException(404, "Pedido não encontrado.")
    envio = db.scalar(select(EnvioProprio).where(EnvioProprio.id_saida == saida.id_saida).limit(1))
    detail = db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == saida.id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )
    dest = _merge_dest(_dest_from_detail(detail), _dest_from_envio(envio))
    resumo = _item_resumo(saida, envio, detail)
    resumo["destinatario"] = dest
    resumo["endereco"] = _endereco_linha(dest)
    timeline = projetar_timeline_seller(db, saida.id_saida, saida, detail)
    resumo["timeline"] = timeline
    resumo["motoboy_nome"] = _nome_entregador_saida(db, saida)
    resumo["atualizado_em"] = (timeline[0].get("quando") if timeline else None) or _iso(
        getattr(saida, "timestamp", None)
    )
    # Seller vê quem recebeu (texto), sem foto/comprovante — prova visual fica com o owner.
    if detail is not None:
        nome_rec = (getattr(detail, "nome_recebedor", None) or "").strip() or None
        tipo_rec = (getattr(detail, "tipo_recebedor", None) or "").strip() or None
        if nome_rec:
            resumo["recebimento"] = {"nome": nome_rec, "tipo": tipo_rec}
        else:
            resumo["recebimento"] = None
    else:
        resumo["recebimento"] = None
    resumo["tem_comprovante"] = False
    return resumo
