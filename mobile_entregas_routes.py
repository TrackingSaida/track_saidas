"""
Rotas do App Motoboy (mobile).
Prefixo: /mobile
Requer JWT de motoboy (role=4, motoboy_id no token).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Optional, List

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db import get_db
from auth import get_current_user
from geocode_utils import geocode_address
from models import (
    User,
    Saida,
    SaidaDetail,
    Motoboy,
    MotoboySubBase,
    MotivoAusencia,
    SaidaHistorico,
    RotasMotoboy,
)
from saidas_routes import (
    STATUS_SAIU_PARA_ENTREGA,
    STATUS_EM_ROTA,
    STATUS_ENTREGUE,
    STATUS_AUSENTE,
    STATUS_CANCELADO,
    _should_store_qr_payload_raw,
    normalizar_status_saida,
    _get_motoboy_nome,
)
from codigo_normalizer import normalize_codigo

router = APIRouter(prefix="/mobile", tags=["Mobile - Entregas"])


# ============================================================
# Dep: usuário deve ser motoboy (role=4, motoboy_id no token)
# ============================================================
def get_current_motoboy(user: User = Depends(get_current_user)) -> User:
    if getattr(user, "role", 0) != 4:
        raise HTTPException(status_code=403, detail="Acesso restrito a motoboys.")
    if not getattr(user, "motoboy_id", None):
        raise HTTPException(status_code=403, detail="Token inválido para motoboy.")
    return user


# ============================================================
# Schemas
# ============================================================
class EntregaListItem(BaseModel):
    id_saida: int
    codigo: Optional[str]
    status: str
    exibicao: str  # "Pendente" | "Entregue" | "Ausente"
    servico: Optional[str] = None  # Shopee | Mercado Livre | Flex | Avulso
    cliente: Optional[str] = None
    bairro: Optional[str] = None
    endereco: Optional[str] = None
    contato: Optional[str] = None
    data: Optional[date] = None
    data_hora_entrega: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    endereco_formatado: Optional[str] = None
    endereco_origem: Optional[str] = None  # manual | ocr | voz
    possui_endereco: bool = False


class ScanBody(BaseModel):
    codigo: str = Field(min_length=1)


class AusenteBody(BaseModel):
    motivo_id: int
    observacao: Optional[str] = None


class EntregueBody(BaseModel):
    tipo_recebedor: Optional[str] = None
    nome_recebedor: Optional[str] = None
    tipo_documento: Optional[str] = None
    numero_documento: Optional[str] = None
    observacao_entrega: Optional[str] = None


class EnderecoBody(BaseModel):
    destinatario: str = Field(min_length=1)
    rua: str = Field(min_length=1)
    numero: str = Field(min_length=1)
    complemento: Optional[str] = None
    bairro: str = Field(min_length=1)
    cidade: str = Field(min_length=1)
    estado: str = Field(min_length=1)
    cep: str = Field(min_length=8)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    origem: str = "manual"  # manual | ocr | voz


class IniciarRotaBody(BaseModel):
    delivery_ids: Optional[List[int]] = None  # se enviado, só esses id_saida vão para EM_ROTA


class MotivoAusenciaOut(BaseModel):
    id: int
    descricao: str


class RotasIniciarBody(BaseModel):
    ordem: List[int] = Field(..., min_length=1)


class RotasIniciarOut(BaseModel):
    rota_id: str


class RotasAvancarOut(BaseModel):
    parada_atual: int


class RotasAtivaOut(BaseModel):
    rota_id: str
    ordem: List[int]
    parada_atual: int
    data: Optional[str] = None


# ============================================================
# Helpers
# ============================================================
def _status_exibicao(status: Optional[str]) -> str:
    if not status:
        return "Pendente"
    s = (status or "").strip().upper()
    if s in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA):
        return "Pendente"
    if s == STATUS_ENTREGUE:
        return "Entregue"
    if s == STATUS_AUSENTE:
        return "Ausente"
    if s == STATUS_CANCELADO:
        return "Cancelado"
    return status or "Pendente"


def _get_saida_for_motoboy(db: Session, id_saida: int, motoboy_id: int, sub_base: str) -> Saida:
    obj = db.get(Saida, id_saida)
    if not obj or obj.sub_base != sub_base or obj.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    return obj


def _get_detail_for_saida(db: Session, id_saida: int) -> Optional[SaidaDetail]:
    return db.scalar(
        select(SaidaDetail)
        .where(SaidaDetail.id_saida == id_saida)
        .order_by(SaidaDetail.id_detail.desc())
        .limit(1)
    )


def _servico_tipo(serv: Optional[str]) -> str:
    """Retorna Shopee | Flex | Avulso para exibição."""
    s = (serv or "").strip().lower()
    if "shopee" in s:
        return "Shopee"
    if "mercado" in s or "ml" in s or "flex" in s:
        return "Flex"
    return "Avulso"


def _possui_endereco(detail: Optional[SaidaDetail]) -> bool:
    if not detail:
        return False
    if detail.endereco_formatado and detail.endereco_formatado.strip():
        return True
    return bool((detail.dest_rua or "").strip() and (detail.dest_numero or "").strip())


def _saida_to_item(s: Saida, detail: Optional[SaidaDetail]) -> dict:
    endereco = None
    if detail and (detail.dest_rua or detail.dest_numero):
        parts = [p for p in [detail.dest_rua, detail.dest_numero, detail.dest_complemento] if p]
        endereco = ", ".join(parts) if parts else None
    lat = float(detail.latitude) if detail and detail.latitude is not None else None
    lon = float(detail.longitude) if detail and detail.longitude is not None else None
    return {
        "id_saida": s.id_saida,
        "codigo": s.codigo,
        "status": s.status or "",
        "exibicao": _status_exibicao(s.status),
        "servico": s.servico,
        "cliente": detail.dest_nome if detail else None,
        "bairro": detail.dest_bairro if detail else None,
        "endereco": endereco,
        "contato": detail.dest_contato if detail else None,
        "data": s.data,
        "data_hora_entrega": s.data_hora_entrega,
        "latitude": lat,
        "longitude": lon,
        "endereco_formatado": (detail.endereco_formatado or "").strip() or None if detail else None,
        "endereco_origem": (detail.endereco_origem or "").strip() or None if detail else None,
        "possui_endereco": _possui_endereco(detail),
    }


# ============================================================
# GET /mobile/entregas
# ============================================================
@router.get("/entregas", response_model=List[EntregaListItem])
def listar_entregas(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Lista entregas do motoboy. status=pendente | finalizadas | ausentes.
    Regra: pendentes e ausentes são listados SEM filtro por data; só somem com ação final (entregue/ausente/cancelado).
    """
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    q = select(Saida).where(
        Saida.sub_base == sub_base,
        Saida.motoboy_id == motoboy_id,
    )
    if status == "pendente":
        q = q.where(Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]))
    elif status == "finalizadas":
        q = q.where(Saida.status == STATUS_ENTREGUE)
    elif status == "ausentes":
        q = q.where(Saida.status == STATUS_AUSENTE)
    q = q.order_by(Saida.data.desc(), Saida.timestamp.desc())

    rows = db.scalars(q).all()
    out = []
    for s in rows:
        detail = _get_detail_for_saida(db, s.id_saida)
        out.append(_saida_to_item(s, detail))
    return out


# ============================================================
# GET /mobile/entregas/resumo
# ============================================================
@router.get("/entregas/resumo")
def resumo_entregas(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Contadores: pendentes, finalizadas_hoje, ausentes, atraso_d1.
    Regra: pendentes e ausentes não usam filtro por 'hoje'; só finalizadas_hoje é por dia.
    """
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    hoje = date.today()
    pendentes = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
        )
    ) or 0
    finalizadas_hoje = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_ENTREGUE,
            func.date(Saida.data_hora_entrega) == hoje,
        )
    ) or 0
    tem_saiu_para_entrega = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_SAIU_PARA_ENTREGA,
        )
    ) or 0
    ausentes = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_AUSENTE,
        )
    ) or 0
    atraso_d1 = db.scalar(
        select(func.count(Saida.id_saida)).where(
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status.in_([STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA]),
            Saida.data < hoje,
        )
    ) or 0

    return {
        "pendentes": pendentes,
        "finalizadas_hoje": finalizadas_hoje,
        "pode_iniciar_rota": tem_saiu_para_entrega > 0,
        "ausentes": ausentes,
        "atraso_d1": atraso_d1,
    }


# ============================================================
# POST /mobile/iniciar-rota
# ============================================================
@router.post("/iniciar-rota")
def iniciar_rota(
    body: Optional[IniciarRotaBody] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Atualiza SAIU_PARA_ENTREGA para EM_ROTA. Se body.delivery_ids for enviado, só esses id_saida; senão, todas do motoboy."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    if body and body.delivery_ids:
        ids = body.delivery_ids
        result = db.execute(
            select(Saida).where(
                Saida.id_saida.in_(ids),
                Saida.sub_base == sub_base,
                Saida.motoboy_id == motoboy_id,
                Saida.status == STATUS_SAIU_PARA_ENTREGA,
            )
        )
        rows = result.scalars().all()
    else:
        result = db.execute(
            select(Saida).where(
                Saida.sub_base == sub_base,
                Saida.motoboy_id == motoboy_id,
                Saida.status == STATUS_SAIU_PARA_ENTREGA,
            )
        )
        rows = result.scalars().all()
    for s in rows:
        s.status = STATUS_EM_ROTA
    db.commit()
    return {"atualizados": len(rows)}


# ============================================================
# POST /mobile/rotas/iniciar
# ============================================================
@router.post("/rotas/iniciar", response_model=RotasIniciarOut)
def rotas_iniciar(
    body: RotasIniciarBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Cria rota ativa com a ordem enviada. Atualiza saidas para EM_ROTA e persiste a rota."""
    motoboy_id = user.motoboy_id
    sub_base = user.sub_base
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    ids = body.ordem
    result = db.execute(
        select(Saida).where(
            Saida.id_saida.in_(ids),
            Saida.sub_base == sub_base,
            Saida.motoboy_id == motoboy_id,
            Saida.status == STATUS_SAIU_PARA_ENTREGA,
        )
    )
    rows = result.scalars().all()
    if len(rows) != len(ids):
        raise HTTPException(
            status_code=400,
            detail="Alguma entrega não pertence ao motoboy ou não está em SAIU_PARA_ENTREGA.",
        )
    for s in rows:
        s.status = STATUS_EM_ROTA

    hoje = date.today()
    rota = RotasMotoboy(
        motoboy_id=motoboy_id,
        data=hoje,
        status="ativa",
        ordem_json=json.dumps(ids),
        parada_atual=0,
        iniciado_em=datetime.utcnow(),
    )
    db.add(rota)
    db.commit()
    db.refresh(rota)
    return RotasIniciarOut(rota_id=str(rota.id))


# ============================================================
# GET /mobile/rotas/ativa
# ============================================================
@router.get("/rotas/ativa", response_model=Optional[RotasAtivaOut])
def rotas_ativa(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Retorna a rota ativa do motoboy, se existir. Caso contrário 200 com null."""
    motoboy_id = user.motoboy_id
    hoje = date.today()
    rota = db.scalar(
        select(RotasMotoboy).where(
            RotasMotoboy.motoboy_id == motoboy_id,
            RotasMotoboy.status == "ativa",
            RotasMotoboy.data == hoje,
        ).order_by(RotasMotoboy.iniciado_em.desc()).limit(1)
    )
    if not rota:
        return None
    ordem = json.loads(rota.ordem_json) if isinstance(rota.ordem_json, str) else rota.ordem_json
    return RotasAtivaOut(
        rota_id=str(rota.id),
        ordem=ordem,
        parada_atual=rota.parada_atual,
        data=rota.data.isoformat() if rota.data else None,
    )


# ============================================================
# POST /mobile/rotas/{id}/avancar
# ============================================================
@router.post("/rotas/{rota_id}/avancar", response_model=RotasAvancarOut)
def rotas_avancar(
    rota_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Incrementa parada_atual da rota. A rota deve pertencer ao motoboy e estar ativa."""
    motoboy_id = user.motoboy_id
    rota = db.get(RotasMotoboy, rota_id)
    if not rota or rota.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Rota não encontrada.")
    if rota.status != "ativa":
        raise HTTPException(status_code=400, detail="Rota não está ativa.")
    rota.parada_atual = rota.parada_atual + 1
    db.commit()
    db.refresh(rota)
    return RotasAvancarOut(parada_atual=rota.parada_atual)


# ============================================================
# POST /mobile/rotas/{id}/finalizar
# ============================================================
@router.post("/rotas/{rota_id}/finalizar", status_code=204)
def rotas_finalizar(
    rota_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca a rota como finalizada."""
    motoboy_id = user.motoboy_id
    rota = db.get(RotasMotoboy, rota_id)
    if not rota or rota.motoboy_id != motoboy_id:
        raise HTTPException(status_code=404, detail="Rota não encontrada.")
    if rota.status != "ativa":
        raise HTTPException(status_code=400, detail="Rota não está ativa.")
    rota.status = "finalizada"
    rota.finalizado_em = datetime.utcnow()
    db.commit()


# ============================================================
# GET /mobile/entrega/{id}
# ============================================================
@router.get("/entrega/{id_saida}", response_model=EntregaListItem)
def detalhe_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Detalhe de uma entrega para o app."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    detail = _get_detail_for_saida(db, s.id_saida)
    return _saida_to_item(s, detail)


# ============================================================
# PUT /mobile/entrega/{id_saida}/endereco
# ============================================================
@router.put("/entrega/{id_saida}/endereco", response_model=EntregaListItem)
def atualizar_endereco(
    id_saida: int,
    body: EnderecoBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Atualiza endereço da entrega (SaidaDetail). Cria detail se não existir."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    detail = _get_detail_for_saida(db, id_saida)
    origem = (body.origem or "manual").strip().lower()
    if origem not in ("manual", "ocr", "voz"):
        origem = "manual"
    parts = [body.rua, body.numero, body.complemento, body.bairro, body.cidade, body.estado, body.cep]
    endereco_formatado = ", ".join(p for p in parts if p)

    lat = body.latitude
    lon = body.longitude
    if (lat is None or lon is None) and endereco_formatado.strip():
        coords = geocode_address(endereco_formatado)
        if coords:
            lat, lon = coords
        else:
            logging.getLogger(__name__).warning(
                "Endereço salvo sem coordenadas (geocoding falhou ou sem resultado): id_saida=%s",
                id_saida,
            )

    if detail:
        detail.dest_nome = body.destinatario.strip()
        detail.dest_rua = body.rua.strip()
        detail.dest_numero = str(body.numero).strip()
        detail.dest_complemento = (body.complemento or "").strip() or None
        detail.dest_bairro = body.bairro.strip()
        detail.dest_cidade = body.cidade.strip()
        detail.dest_estado = body.estado.strip()
        detail.dest_cep = body.cep.strip()
        detail.endereco_formatado = endereco_formatado
        detail.endereco_origem = origem
        if lat is not None:
            detail.latitude = lat
        if lon is not None:
            detail.longitude = lon
    else:
        detail = SaidaDetail(
            id_saida=id_saida,
            id_entregador=user.motoboy_id,
            status=s.status or STATUS_EM_ROTA,
            tentativa=1,
            dest_nome=body.destinatario.strip(),
            dest_rua=body.rua.strip(),
            dest_numero=str(body.numero).strip(),
            dest_complemento=(body.complemento or "").strip() or None,
            dest_bairro=body.bairro.strip(),
            dest_cidade=body.cidade.strip(),
            dest_estado=body.estado.strip(),
            dest_cep=body.cep.strip(),
            endereco_formatado=endereco_formatado,
            endereco_origem=origem,
            latitude=lat,
            longitude=lon,
        )
        db.add(detail)
    db.commit()
    db.refresh(detail)
    return _saida_to_item(s, detail)


# ============================================================
# POST /mobile/entrega/{id}/entregue
# ============================================================
@router.post("/entrega/{id_saida}/entregue")
def marcar_entregue(
    id_saida: int,
    body: Optional[EntregueBody] = Body(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca entrega como ENTREGUE e registra data_hora_entrega. Só permite se status for EM_ROTA.
    Se body for enviado, preenche tipo_recebedor, nome_recebedor, tipo_documento, numero_documento, observacao_entrega em saidas_detail."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)
    if status_norm == STATUS_SAIU_PARA_ENTREGA:
        raise HTTPException(
            status_code=422,
            detail="Inicie a rota antes de finalizar entregas.",
        )

    if body:
        def _set_if_present(detail: SaidaDetail) -> None:
            if body.tipo_recebedor is not None:
                detail.tipo_recebedor = (body.tipo_recebedor or "").strip() or None
            if body.nome_recebedor is not None:
                detail.nome_recebedor = (body.nome_recebedor or "").strip() or None
            if body.tipo_documento is not None:
                detail.tipo_documento = (body.tipo_documento or "").strip() or None
            if body.numero_documento is not None:
                detail.numero_documento = (body.numero_documento or "").strip() or None
            if body.observacao_entrega is not None:
                detail.observacao_entrega = (body.observacao_entrega or "").strip() or None

        detail = _get_detail_for_saida(db, id_saida)
        if detail:
            _set_if_present(detail)
        else:
            detail = SaidaDetail(
                id_saida=id_saida,
                id_entregador=user.motoboy_id,
                status=s.status or STATUS_EM_ROTA,
                tentativa=1,
            )
            _set_if_present(detail)
            db.add(detail)

    s.status = STATUS_ENTREGUE
    s.data_hora_entrega = datetime.utcnow()
    db.commit()
    return {"ok": True, "id_saida": id_saida}


# ============================================================
# POST /mobile/entrega/{id}/ausente
# ============================================================
@router.post("/entrega/{id_saida}/ausente")
def marcar_ausente(
    id_saida: int,
    body: AusenteBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Marca entrega como AUSENTE com motivo. Só permite se status for EM_ROTA."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    status_norm = normalizar_status_saida(s.status)
    if status_norm == STATUS_SAIU_PARA_ENTREGA:
        raise HTTPException(
            status_code=422,
            detail="Inicie a rota antes de finalizar entregas.",
        )
    motivo = db.get(MotivoAusencia, body.motivo_id)
    if not motivo or not motivo.ativo:
        raise HTTPException(status_code=422, detail="Motivo de ausência inválido.")
    if motivo.descricao.strip().lower() == "outro" and not (body.observacao or "").strip():
        raise HTTPException(status_code=422, detail="Observação obrigatória quando motivo é 'Outro'.")

    s.status = STATUS_AUSENTE
    detail = _get_detail_for_saida(db, id_saida)
    if detail:
        detail.motivo_ocorrencia = motivo.descricao
        detail.observacao_ocorrencia = (body.observacao or "").strip() or None
    else:
        detail = SaidaDetail(
            id_saida=id_saida,
            id_entregador=0,
            status=STATUS_AUSENTE,
            motivo_ocorrencia=motivo.descricao,
            observacao_ocorrencia=(body.observacao or "").strip() or None,
        )
        db.add(detail)
    db.commit()
    return {"ok": True, "id_saida": id_saida}


# ============================================================
# GET /mobile/motivos-ausencia
# ============================================================
@router.get("/motivos-ausencia", response_model=List[MotivoAusenciaOut])
def listar_motivos_ausencia(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Lista motivos de ausência ativos para o combo."""
    rows = db.scalars(
        select(MotivoAusencia).where(MotivoAusencia.ativo.is_(True)).order_by(MotivoAusencia.id)
    ).all()
    return [MotivoAusenciaOut(id=r.id, descricao=r.descricao) for r in rows]


# ============================================================
# POST /mobile/scan — leituras sequenciais (igual web): INSERT novo ou atribui existente
# ============================================================
def _nome_motoboy_atual(db: Session, saida: Saida) -> str:
    if not saida or not saida.motoboy_id:
        return "Motoboy"
    motoboy = db.get(Motoboy, saida.motoboy_id)
    return _get_motoboy_nome(db, motoboy) if motoboy else "Motoboy"


@router.post("/scan")
def scan_codigo(
    body: ScanBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """
    Leituras sequenciais (igual web): se código não existe -> INSERT novo e atribui ao motoboy.
    Se existe: valida status (não permite cancelado, entregue, em_rota de outro).
    Retorna status na resposta de erro quando bloqueia por status.
    """
    raw = body.codigo.strip()
    sub_base = user.sub_base
    motoboy_id = user.motoboy_id
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")

    codigo, servico, qr_payload_raw = normalize_codigo(raw)
    if codigo is None:
        raise HTTPException(
            status_code=422,
            detail="Código inválido. Verifique o formato do QR/código de barras.",
        )

    saida = db.scalar(
        select(Saida).where(
            Saida.codigo == codigo,
            Saida.sub_base == sub_base,
        )
    )

    # ——— Código não existe: registrar como novo (leitura sequencial, igual web) ———
    if not saida:
        motoboy = db.get(Motoboy, motoboy_id)
        entregador_nome = _get_motoboy_nome(db, motoboy) if motoboy else (user.username or "Motoboy")
        servico_val = (servico or "Avulso").strip().title()
        qr_raw = qr_payload_raw.strip() if (qr_payload_raw and _should_store_qr_payload_raw(servico_val, qr_payload_raw)) else None
        try:
            nova = Saida(
                sub_base=sub_base,
                username=user.username,
                entregador=entregador_nome,
                entregador_id=None,
                motoboy_id=motoboy_id,
                codigo=codigo,
                servico=servico_val,
                status=STATUS_SAIU_PARA_ENTREGA,
                qr_payload_raw=qr_raw or None,
            )
            db.add(nova)
            db.commit()
            db.refresh(nova)
            detail = _get_detail_for_saida(db, nova.id_saida)
            return {"ok": True, "conflito": False, "entrega": _saida_to_item(nova, detail)}
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Erro ao registrar leitura: {e}")

    # ——— Existe: validar status (não permitir cancelado, entregue, em_rota de outro) ———
    status_norm = normalizar_status_saida(saida.status)

    if status_norm == STATUS_CANCELADO:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido cancelado. Não é possível registrar leitura. Status: {STATUS_CANCELADO}.",
        )

    if status_norm == STATUS_ENTREGUE:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido já entregue. Não é possível registrar leitura. Status: {STATUS_ENTREGUE}.",
        )

    # Em rota / saiu com outro motoboy -> conflito (perguntar se quer assumir)
    if status_norm in (STATUS_SAIU_PARA_ENTREGA, STATUS_EM_ROTA, "saiu"):
        if saida.motoboy_id == motoboy_id:
            if qr_payload_raw and _should_store_qr_payload_raw(servico or "", qr_payload_raw):
                if not saida.qr_payload_raw or not saida.qr_payload_raw.strip():
                    saida.qr_payload_raw = qr_payload_raw.strip()
                    db.commit()
            detail = _get_detail_for_saida(db, saida.id_saida)
            return {"ok": True, "conflito": False, "entrega": _saida_to_item(saida, detail)}
        nome_atual = _nome_motoboy_atual(db, saida)
        return JSONResponse(
            status_code=409,
            content={
                "conflito": True,
                "motoboy_atual": nome_atual,
                "id_saida": saida.id_saida,
            },
        )

    # Coletado ou outro: atribuir ao motoboy logado
    if qr_payload_raw and _should_store_qr_payload_raw(servico or "", qr_payload_raw):
        if not saida.qr_payload_raw or not saida.qr_payload_raw.strip():
            saida.qr_payload_raw = qr_payload_raw.strip()
    saida.motoboy_id = motoboy_id
    saida.status = STATUS_SAIU_PARA_ENTREGA
    db.commit()
    db.refresh(saida)
    detail = _get_detail_for_saida(db, saida.id_saida)
    return {"ok": True, "conflito": False, "entrega": _saida_to_item(saida, detail)}


# ============================================================
# POST /mobile/entrega/{id}/desatribuir
# ============================================================
@router.post("/entrega/{id_saida}/desatribuir")
def desatribuir_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Remove atribuição: motoboy_id = null. Apenas para entregas do próprio motoboy."""
    s = _get_saida_for_motoboy(db, id_saida, user.motoboy_id, user.sub_base)
    s.motoboy_id = None
    db.commit()
    return {"ok": True, "id_saida": id_saida}


# ============================================================
# POST /mobile/entrega/{id}/assumir
# ============================================================
@router.post("/entrega/{id_saida}/assumir")
def assumir_entrega(
    id_saida: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_motoboy),
):
    """Reatribui a entrega para o motoboy logado (após conflito no scan). Não permite se cancelado/entregue."""
    s = db.get(Saida, id_saida)
    if not s or s.sub_base != user.sub_base:
        raise HTTPException(status_code=404, detail="Entrega não encontrada.")
    status_norm = normalizar_status_saida(s.status)
    if status_norm == STATUS_CANCELADO:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido cancelado. Não é possível assumir. Status: {STATUS_CANCELADO}.",
        )
    if status_norm == STATUS_ENTREGUE:
        raise HTTPException(
            status_code=422,
            detail=f"Pedido já entregue. Não é possível assumir. Status: {STATUS_ENTREGUE}.",
        )
    if s.motoboy_id == user.motoboy_id:
        return {"ok": True, "id_saida": id_saida}

    antigo = s.motoboy_id
    s.motoboy_id = user.motoboy_id
    s.status = STATUS_SAIU_PARA_ENTREGA
    db.add(
        SaidaHistorico(
            id_saida=id_saida,
            evento="reatribuicao",
            motoboy_id_anterior=antigo,
            motoboy_id_novo=user.motoboy_id,
            user_id=user.id,
        )
    )
    db.commit()
    return {"ok": True, "id_saida": id_saida}
