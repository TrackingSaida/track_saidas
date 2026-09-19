"""Criação e admissão de envios próprios (código RTE global)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from auth import _coerce_role_int
from etiqueta_identidade_service import resolver_contato, resolver_nome_exibicao, resolver_slogan
from etiqueta_pdf_service import gerar_etiqueta
from models import BasePreco, BaseSellerDados, EnvioProprio, Owner, Saida, SaidaDetail, SaidaHistorico, User
from cobertura_cep_service import avaliar_cobertura

MSG_SOMENTE_OWNER_BASE = "Disponível apenas para Owner tipo Base."

logger = logging.getLogger(__name__)
OPERACAO_TZ = ZoneInfo("America/Sao_Paulo")

STATUS_ETIQUETADO = "ETIQUETADO"
STATUS_CANCELADO = "cancelado"
RTE_CODIGO_RE = re.compile(r"^RTE[0-9]{11,}$")
MSG_CEP_FORA_COBERTURA = "CEP fora da área de atendimento."


def is_codigo_rte(codigo: Optional[str]) -> bool:
    return bool(RTE_CODIGO_RE.match((codigo or "").strip().upper()))


def require_owner_tipo_base(db: Session, current_user: User) -> Owner:
    """Envio próprio só para Owner tipo Base. Gerar etiqueta de código existente não usa este gate."""
    sub_base = (getattr(current_user, "sub_base", None) or "").strip()
    if not sub_base:
        raise HTTPException(403, "Usuário sem sub_base definida.")
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    if not owner:
        raise HTTPException(404, "Owner não encontrado para esta sub_base.")
    tipo = (getattr(owner, "tipo_owner", None) or "").strip().lower()
    if tipo != "base":
        raise HTTPException(403, MSG_SOMENTE_OWNER_BASE)
    return owner


def _hoje_operacional() -> datetime:
    return datetime.now(OPERACAO_TZ)


def _next_rte_seq(db: Session) -> int:
    return int(db.execute(text("SELECT nextval('envio_proprio_codigo_seq')")).scalar_one())


def gerar_codigo_rte(db: Session) -> str:
    """Formato compacto RTE{YYMMDD}{SEQ:05d} — unique global via sequence + UNIQUE(codigo)."""
    while True:
        seq = _next_rte_seq(db)
        data = _hoje_operacional().strftime("%y%m%d")
        codigo = f"RTE{data}{seq:05d}"
        existe = db.scalar(select(EnvioProprio.id_envio).where(EnvioProprio.codigo == codigo).limit(1))
        if not existe:
            return codigo


def _require_addr_fields(prefix: str, data: Dict[str, Any], required: Tuple[str, ...]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    missing = []
    for key in required:
        val = (data.get(key) or "").strip() if data.get(key) is not None else ""
        if not val:
            missing.append(f"{prefix}_{key}" if not key.startswith(prefix) else key)
        out[key] = val
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"code": "CAMPOS_OBRIGATORIOS", "message": "Campos obrigatórios ausentes.", "campos": missing},
        )
    return out


def _party_dict_from_envio(envio: EnvioProprio, kind: str) -> Dict[str, Any]:
    if kind == "remetente":
        return {
            "nome": envio.remetente_nome,
            "telefone": envio.remetente_telefone,
            "cep": envio.remetente_cep,
            "rua": envio.remetente_rua,
            "numero": envio.remetente_numero,
            "complemento": envio.remetente_complemento,
            "bairro": envio.remetente_bairro,
            "cidade": envio.remetente_cidade,
            "uf": envio.remetente_uf,
        }
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


def criar_envio_proprio(
    db: Session,
    *,
    current_user: Optional[User] = None,
    payload: Dict[str, Any],
    origem_emissao: str = "staff",
    force_id_base: Optional[int] = None,
    sub_base_override: Optional[str] = None,
    criado_por_user_id: Optional[int] = None,
) -> Tuple[EnvioProprio, Saida, bytes, Optional[str]]:
    origem_emissao = (origem_emissao or "staff").strip().lower()
    if origem_emissao not in ("staff", "portal"):
        origem_emissao = "staff"

    if origem_emissao == "staff":
        if current_user is None:
            raise HTTPException(401, "Não autenticado")
        role = _coerce_role_int(getattr(current_user, "role", None))
        if role not in (0, 1, 2):
            raise HTTPException(403, "Sem permissão para criar envio próprio.")
        owner = require_owner_tipo_base(db, current_user)
        sub_base = (getattr(owner, "sub_base", None) or "").strip()
        username = getattr(current_user, "username", None)
        user_id = getattr(current_user, "id", None)
    else:
        sub_base = (sub_base_override or "").strip()
        if not sub_base:
            raise HTTPException(403, "Usuário sem sub_base definida.")
        owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
        if not owner:
            raise HTTPException(404, "Owner não encontrado para esta sub_base.")
        tipo = (getattr(owner, "tipo_owner", None) or "").strip().lower()
        if tipo != "base":
            raise HTTPException(403, MSG_SOMENTE_OWNER_BASE)
        username = None
        user_id = criado_por_user_id

    origem = (payload.get("origem_remetente") or "").strip().lower()
    if origem_emissao == "portal":
        origem = "seller"
        if force_id_base is None:
            raise HTTPException(422, "id_base é obrigatório.")
        payload["id_base"] = int(force_id_base)
    elif origem not in ("seller", "manual"):
        raise HTTPException(422, "origem_remetente deve ser 'seller' ou 'manual'.")

    id_base = payload.get("id_base")
    remetente: Dict[str, Any] = {}

    if origem == "seller":
        if id_base is None:
            raise HTTPException(422, "id_base é obrigatório quando origem_remetente=seller.")
        base = db.get(BasePreco, int(id_base))
        if not base or (base.sub_base or "").strip() != sub_base:
            raise HTTPException(404, "Seller/Base não encontrado.")
        seller = db.scalar(
            select(BaseSellerDados).where(BaseSellerDados.base_id == int(id_base)).limit(1)
        )
        if not seller:
            raise HTTPException(
                422,
                "Seller sem endereço cadastrado. Complete o cadastro da Base antes de gerar a etiqueta.",
            )
        required = ("rua", "numero", "bairro", "cidade", "cep")
        for f in required:
            if not (getattr(seller, f, None) or "").strip():
                raise HTTPException(
                    422,
                    "Seller sem endereço completo. Complete o cadastro da Base antes de gerar a etiqueta.",
                )
        remetente = {
            "nome": (base.base or "").strip() or "Seller",
            "telefone": (payload.get("remetente_telefone") or "").strip() or None,
            "cep": seller.cep,
            "rua": seller.rua,
            "numero": seller.numero,
            "complemento": seller.complemento,
            "bairro": seller.bairro,
            "cidade": seller.cidade,
            "uf": (seller.estado or "").strip() or "SP",
        }
    else:
        rem_in = payload.get("remetente") or {}
        fields = _require_addr_fields(
            "remetente",
            rem_in,
            ("nome", "cep", "rua", "numero", "bairro", "cidade", "uf"),
        )
        remetente = {
            **fields,
            "telefone": (rem_in.get("telefone") or "").strip() or None,
            "complemento": (rem_in.get("complemento") or "").strip() or None,
        }
        id_base = None

    dest_in = payload.get("destinatario") or {}
    dest_fields = _require_addr_fields(
        "destinatario",
        dest_in,
        ("nome", "cep", "rua", "numero", "bairro", "cidade", "uf"),
    )
    destinatario = {
        **dest_fields,
        "telefone": (dest_in.get("telefone") or "").strip() or None,
        "complemento": (dest_in.get("complemento") or "").strip() or None,
    }

    coberto, _prefixos = avaliar_cobertura(db, sub_base, destinatario.get("cep"))
    cobertura_aviso: Optional[str] = None
    if not coberto:
        if origem_emissao == "portal":
            raise HTTPException(
                status_code=422,
                detail={"code": "CEP_FORA_COBERTURA", "message": MSG_CEP_FORA_COBERTURA},
            )
        cobertura_aviso = MSG_CEP_FORA_COBERTURA

    peso_kg = payload.get("peso_kg")
    if peso_kg is not None:
        try:
            peso_kg = float(peso_kg)
        except (TypeError, ValueError):
            raise HTTPException(422, "peso_kg inválido.")
    dimensoes = (payload.get("dimensoes") or "").strip() or None
    observacao = (payload.get("observacao") or "").strip() or None

    codigo = gerar_codigo_rte(db)
    nome_exib = resolver_nome_exibicao(owner)
    slogan = resolver_slogan(owner)
    logo_key = (getattr(owner, "logo_object_key", None) or "").strip() or None

    saida = Saida(
        sub_base=sub_base,
        username=username,
        base=(remetente.get("nome") or "").strip() or None,
        codigo=codigo,
        servico="Avulso",
        status=STATUS_ETIQUETADO,
        entregador_id=None,
        entregador=None,
        motoboy_id=None,
        id_coleta=None,
    )
    db.add(saida)
    db.flush()

    envio = EnvioProprio(
        sub_base=sub_base,
        owner_id=owner.id_owner,
        id_saida=saida.id_saida,
        codigo=codigo,
        origem_remetente=origem,
        id_base=int(id_base) if id_base is not None else None,
        remetente_nome=remetente["nome"],
        remetente_telefone=remetente.get("telefone"),
        remetente_cep=remetente["cep"],
        remetente_rua=remetente["rua"],
        remetente_numero=remetente["numero"],
        remetente_complemento=remetente.get("complemento"),
        remetente_bairro=remetente["bairro"],
        remetente_cidade=remetente["cidade"],
        remetente_uf=remetente["uf"],
        dest_nome=destinatario["nome"],
        dest_telefone=destinatario.get("telefone"),
        dest_cep=destinatario["cep"],
        dest_rua=destinatario["rua"],
        dest_numero=destinatario["numero"],
        dest_complemento=destinatario.get("complemento"),
        dest_bairro=destinatario["bairro"],
        dest_cidade=destinatario["cidade"],
        dest_uf=destinatario["uf"],
        peso_kg=Decimal(str(peso_kg)) if peso_kg is not None else None,
        dimensoes=dimensoes,
        observacao=observacao,
        owner_nome_exibicao=nome_exib,
        owner_slogan=slogan or None,
        logo_object_key_used=logo_key,
        criado_por_user_id=user_id,
        origem_emissao=origem_emissao,
    )
    db.add(envio)

    db.add(
        SaidaDetail(
            id_saida=saida.id_saida,
            id_entregador=0,
            status=STATUS_ETIQUETADO,
            tentativa=1,
            dest_nome=destinatario["nome"],
            dest_rua=destinatario["rua"],
            dest_numero=destinatario["numero"],
            dest_complemento=destinatario.get("complemento"),
            dest_bairro=destinatario["bairro"],
            dest_cidade=destinatario["cidade"],
            dest_estado=destinatario["uf"],
            dest_cep=destinatario["cep"],
            dest_contato=destinatario.get("telefone"),
        )
    )
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento="etiqueta_gerada",
            status_novo=STATUS_ETIQUETADO,
            user_id=user_id,
        )
    )
    db.flush()

    pdf = gerar_etiqueta(
        modo="envio_proprio",
        codigo=codigo,
        formato="pdf",
        owner=owner,
        remetente=remetente,
        destinatario=destinatario,
        peso_kg=peso_kg,
        dimensoes=dimensoes,
        created_at=envio.created_at or datetime.utcnow(),
        nome_exibicao_override=nome_exib,
        slogan_override=slogan,
        logo_key_hint=logo_key,
        observacao=observacao,
        contato_override=resolver_contato(owner),
    )
    db.commit()
    db.refresh(envio)
    db.refresh(saida)
    return envio, saida, pdf, cobertura_aviso


def pdf_from_envio(db: Session, envio: EnvioProprio) -> bytes:
    owner = db.get(Owner, envio.owner_id) if envio.owner_id else None
    if owner is None:
        owner = db.scalar(select(Owner).where(Owner.sub_base == envio.sub_base))
    peso = float(envio.peso_kg) if envio.peso_kg is not None else None
    return gerar_etiqueta(
        modo="envio_proprio",
        codigo=envio.codigo,
        formato="pdf",
        owner=owner,
        remetente=_party_dict_from_envio(envio, "remetente"),
        destinatario=_party_dict_from_envio(envio, "destinatario"),
        peso_kg=peso,
        dimensoes=envio.dimensoes,
        created_at=envio.created_at,
        nome_exibicao_override=envio.owner_nome_exibicao,
        slogan_override=envio.owner_slogan or "",
        logo_key_hint=envio.logo_object_key_used,
        observacao=envio.observacao,
        contato_override=resolver_contato(owner),
    )


def get_envio_by_codigo_global(db: Session, codigo: str) -> Optional[EnvioProprio]:
    cod = (codigo or "").strip().upper()
    if not is_codigo_rte(cod):
        return None
    return db.scalar(select(EnvioProprio).where(EnvioProprio.codigo == cod).limit(1))


def admitir_envio_proprio_no_tenant(
    db: Session,
    *,
    sub_base: str,
    codigo: str,
    status_inicial: str,
    username: Optional[str] = None,
    base: Optional[str] = None,
    user_id: Optional[int] = None,
    evento_historico: str = "lido",
) -> Optional[Saida]:
    """
    Materializa Saida Avulso no tenant atual a partir do snapshot global.
    Nunca altera Saida de outro tenant.
    Retorna Saida local criada, ou None se código RTE sem snapshot.
    """
    sub = (sub_base or "").strip()
    cod = (codigo or "").strip().upper()
    if not sub or not is_codigo_rte(cod):
        return None

    local = db.scalar(
        select(Saida).where(Saida.sub_base == sub, Saida.codigo == cod).limit(1)
    )
    if local is not None:
        return local

    envio = get_envio_by_codigo_global(db, cod)
    if envio is None:
        return None

    saida = Saida(
        sub_base=sub,
        username=username,
        base=(base or envio.remetente_nome or "").strip() or None,
        codigo=cod,
        servico="Avulso",
        status=status_inicial,
    )
    db.add(saida)
    db.flush()
    db.add(
        SaidaDetail(
            id_saida=saida.id_saida,
            id_entregador=0,
            status=status_inicial,
            tentativa=1,
            dest_nome=envio.dest_nome,
            dest_rua=envio.dest_rua,
            dest_numero=envio.dest_numero,
            dest_complemento=envio.dest_complemento,
            dest_bairro=envio.dest_bairro,
            dest_cidade=envio.dest_cidade,
            dest_estado=envio.dest_uf,
            dest_cep=envio.dest_cep,
            dest_contato=envio.dest_telefone,
        )
    )
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento=evento_historico,
            status_novo=status_inicial,
            user_id=user_id,
            payload=None,
        )
    )
    return saida


def status_etiqueta_amigavel(status: Optional[str]) -> str:
    st = (status or "").strip().lower()
    if st == "etiquetado":
        return "Aguardando coleta"
    if st == "cancelado":
        return "Cancelada"
    if st in ("coletado", "saiu", "saiu pra entrega", "saiu_pra_entrega", "saiu_para_entrega", "em_rota"):
        return "Coletado"
    if st == "entregue":
        return "Entregue"
    if not st:
        return "Aguardando coleta"
    return "Coletado"


def cancelar_envio_proprio(
    db: Session,
    envio: EnvioProprio,
    *,
    cancelado_por: str,
    saida: Optional[Saida] = None,
) -> EnvioProprio:
    if saida is None and envio.id_saida:
        saida = db.get(Saida, envio.id_saida)
    st = (getattr(saida, "status", None) or "").strip().upper() if saida else ""
    if saida is None or st != STATUS_ETIQUETADO:
        raise HTTPException(409, "Só é possível cancelar etiqueta ainda aguardando coleta.")
    agora = datetime.utcnow()
    saida.status = STATUS_CANCELADO
    envio.cancelado_at = agora
    envio.cancelado_por = (cancelado_por or "")[:120] or None
    db.add(
        SaidaHistorico(
            id_saida=saida.id_saida,
            evento="etiqueta_cancelada",
            status_anterior=STATUS_ETIQUETADO,
            status_novo=STATUS_CANCELADO,
        )
    )
    db.commit()
    db.refresh(envio)
    return envio


def limite_diario_para_seller(db: Session, sub_base: str, id_base: int) -> int:
    owner = db.scalar(select(Owner).where(Owner.sub_base == (sub_base or "").strip()))
    default = int(getattr(owner, "etiqueta_limite_diario_default", None) or 50)
    base = db.get(BasePreco, int(id_base)) if id_base else None
    if base is not None and getattr(base, "etiqueta_limite_diario", None) is not None:
        try:
            return max(0, int(base.etiqueta_limite_diario))
        except (TypeError, ValueError):
            return default
    return max(0, default)


def contar_emissoes_hoje(db: Session, id_base: int) -> int:
    hoje = datetime.now(OPERACAO_TZ).date()
    inicio = datetime(hoje.year, hoje.month, hoje.day)
    return int(
        db.scalar(
            select(func.count(EnvioProprio.id_envio)).where(
                EnvioProprio.id_base == int(id_base),
                EnvioProprio.created_at >= inicio,
                EnvioProprio.cancelado_at.is_(None),
            )
        )
        or 0
    )


def assert_limite_diario(db: Session, sub_base: str, id_base: int) -> None:
    limite = limite_diario_para_seller(db, sub_base, id_base)
    usados = contar_emissoes_hoje(db, id_base)
    if usados >= limite:
        raise HTTPException(
            409,
            f"Limite diário de {limite} etiquetas atingido para hoje.",
        )


def expirar_etiquetas_etiquetado(db: Session, *, max_rows: int = 500) -> Dict[str, Any]:
    owners = list(db.scalars(select(Owner).where(Owner.sub_base.is_not(None))).all())
    total = 0
    for owner in owners:
        dias = int(getattr(owner, "etiqueta_expiracao_dias", None) or 30)
        if dias < 1:
            dias = 30
        corte = datetime.utcnow() - timedelta(days=dias)
        sub = (owner.sub_base or "").strip()
        envios = list(
            db.scalars(
                select(EnvioProprio)
                .where(
                    EnvioProprio.sub_base == sub,
                    EnvioProprio.cancelado_at.is_(None),
                    EnvioProprio.created_at < corte,
                )
                .limit(max_rows)
            ).all()
        )
        for envio in envios:
            saida = db.get(Saida, envio.id_saida) if envio.id_saida else None
            st = (getattr(saida, "status", None) or "").strip().upper() if saida else ""
            if saida is None or st != STATUS_ETIQUETADO:
                continue
            saida.status = STATUS_CANCELADO
            envio.cancelado_at = datetime.utcnow()
            envio.cancelado_por = "expiracao"
            db.add(
                SaidaHistorico(
                    id_saida=saida.id_saida,
                    evento="etiqueta_expirada",
                    status_anterior=STATUS_ETIQUETADO,
                    status_novo=STATUS_CANCELADO,
                )
            )
            total += 1
    db.commit()
    return {"expirados": total}
