"""Registrar Entrada na base (sem seller/cobrança de coleta)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import get_current_user
from codigo_normalizer import canonicalize_servico, is_qr_like_scan_payload, normalize_codigo
from db import get_db
from leitura_manual_auth import ensure_manual_code_entry_allowed
from models import Owner, Saida, SaidaDetail, SaidaHistorico, User
from qr_payload_utils import (
    apply_qr_payload_if_needed,
    needs_qr_update,
    qr_flags_for_response,
    should_store_qr_payload_raw,
)
from saidas_routes import (
    STATUS_NA_BASE,
    _gerar_codigo_avulso,
    _normalizar_label_avulso,
    normalizar_status_saida,
)

router = APIRouter(prefix="/entradas", tags=["Entradas"])

MSG_ENTRADA_OBRIGATORIA = "Este pacote ainda não teve entrada na base."
OPERACAO_TZ = ZoneInfo("America/Sao_Paulo")


class EntradaLerIn(BaseModel):
    codigo: str = Field(min_length=1)
    origem: Optional[str] = None
    qr_payload_raw: Optional[str] = None


class EntradaLancarAvulsoIn(BaseModel):
    identificacao: Optional[str] = Field(default=None, max_length=32)
    quantidade: int = Field(default=1, ge=1, le=50)
    # Foto opcional (root/admin nunca obrigatória; staff operação sem flag de motoboy).
    foto_object_key: Optional[str] = Field(default=None, max_length=500)
    foto_object_keys: Optional[List[str]] = None
    photo_id: Optional[str] = Field(default=None, max_length=80)
    photo_ids: Optional[List[Optional[str]]] = None


class EntradaLancarAvulsoOut(BaseModel):
    quantidade_criada: int
    codigos: List[str]
    saidas: List[dict]
    mensagem: str


class EntradaResumoDiaOut(BaseModel):
    data_ref: date
    total: int
    sum_shopee: int
    sum_mercado: int
    sum_avulso: int


def _owner_entrada_habilitada(db: Session, sub_base: str, user: User) -> bool:
    if bool(getattr(user, "entrada_obrigatoria_habilitada", False)):
        return True
    owner = db.scalar(select(Owner).where(Owner.sub_base == sub_base))
    return bool(owner and getattr(owner, "entrada_obrigatoria_habilitada", False))


def _require_staff_entrada(db: Session, current_user: User) -> str:
    role = int(getattr(current_user, "role", 0) or 0)
    if role not in (0, 1, 2, 3):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin/operador.")
    sub_base = (current_user.sub_base or "").strip()
    if not sub_base:
        raise HTTPException(status_code=403, detail="Sub-base não definida.")
    if not _owner_entrada_habilitada(db, sub_base, current_user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ENTRADA_DESABILITADA",
                "message": "Registrar entrada não está habilitado para esta base.",
            },
        )
    return sub_base


def _servico_bucket(servico: Optional[str]) -> str:
    s = (servico or "").strip().lower()
    if "shopee" in s:
        return "shopee"
    if s in ("ml",) or "mercado" in s or "livre" in s:
        return "ml"
    return "avulso"


def _bounds_dia_operacional(data_ref: date) -> tuple[datetime, datetime]:
    start = datetime.combine(data_ref, time.min, tzinfo=OPERACAO_TZ)
    end = start + timedelta(days=1)
    # Historico tipicamente naive; compara em horário de Brasília (naive).
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def _entrada_ok_payload(row: Saida, *, ja_existia: bool, promovido_coleta: bool = False, qr_result: Optional[dict] = None) -> dict:
    out = {
        "ok": True,
        "ja_existia": ja_existia,
        "id_saida": row.id_saida,
        "codigo": row.codigo,
        "servico": row.servico,
        "status": row.status,
    }
    if promovido_coleta:
        out["promovido_coleta"] = True
    if qr_result:
        out.update(qr_flags_for_response(qr_result))
    return out


@router.get("/resumo-dia", response_model=EntradaResumoDiaOut)
def resumo_entradas_dia(
    data_ref: Optional[date] = Query(None, description="Dia operacional (YYYY-MM-DD). Default: hoje."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Totais de entrada na base no dia (todos os operadores da sub_base)."""
    sub_base = _require_staff_entrada(db, current_user)
    dia = data_ref or datetime.now(OPERACAO_TZ).date()
    start, end = _bounds_dia_operacional(dia)

    rows = db.execute(
        select(Saida.servico, func.count(func.distinct(SaidaHistorico.id_saida)))
        .join(Saida, Saida.id_saida == SaidaHistorico.id_saida)
        .where(
            Saida.sub_base == sub_base,
            SaidaHistorico.evento == "entrada_base",
            SaidaHistorico.timestamp >= start,
            SaidaHistorico.timestamp < end,
        )
        .group_by(Saida.servico)
    ).all()

    shopee = ml = avulso = 0
    for servico, qtd in rows:
        n = int(qtd or 0)
        bucket = _servico_bucket(servico)
        if bucket == "shopee":
            shopee += n
        elif bucket == "ml":
            ml += n
        else:
            avulso += n

    return EntradaResumoDiaOut(
        data_ref=dia,
        total=shopee + ml + avulso,
        sum_shopee=shopee,
        sum_mercado=ml,
        sum_avulso=avulso,
    )


@router.post("/ler")
def ler_entrada(
    payload: EntradaLerIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sub_base = _require_staff_entrada(db, current_user)

    origem = ensure_manual_code_entry_allowed(
        db, current_user, origem=getattr(payload, "origem", None) or "camera"
    )
    strict_qr = origem == "camera"
    raw = (payload.codigo or "").strip()
    if strict_qr and not is_qr_like_scan_payload(raw):
        raise HTTPException(
            status_code=422,
            detail="Leitura inválida pela câmera. Use apenas QRCode da etiqueta.",
        )

    codigo, servico, qr_from_norm = normalize_codigo(raw, strict_qr=strict_qr)
    if codigo is None or servico is None:
        raise HTTPException(
            status_code=422,
            detail="Código inválido. Verifique o formato do QR/código de barras.",
        )

    servico_val = canonicalize_servico(servico)
    qr_payload_raw = payload.qr_payload_raw or qr_from_norm
    store_qr = should_store_qr_payload_raw(servico_val, qr_payload_raw)

    existente = db.scalar(
        select(Saida).where(Saida.sub_base == sub_base, Saida.codigo == codigo)
    )

    if existente is None:
        try:
            row = Saida(
                sub_base=sub_base,
                username=current_user.username,
                codigo=codigo,
                servico=servico_val,
                status=STATUS_NA_BASE,
                qr_payload_raw=qr_payload_raw.strip() if store_qr and qr_payload_raw else None,
            )
            db.add(row)
            db.flush()
            db.add(
                SaidaHistorico(
                    id_saida=row.id_saida,
                    evento="entrada_base",
                    status_novo=STATUS_NA_BASE,
                    user_id=getattr(current_user, "id", None),
                )
            )
            # Flags (alerta se ML sem JSON completo); payload já gravado no INSERT quando válido.
            qr_result = apply_qr_payload_if_needed(row, None, servico_val)
            db.commit()
            return _entrada_ok_payload(row, ja_existia=False, qr_result=qr_result)
        except Exception as e:
            db.rollback()
            raise HTTPException(500, f"Erro ao registrar entrada: {e}")

    status_norm = normalizar_status_saida(existente.status)

    if status_norm == STATUS_NA_BASE:
        if needs_qr_update(existente, qr_payload_raw, servico_val):
            try:
                qr_result = apply_qr_payload_if_needed(existente, qr_payload_raw, servico_val)
                db.commit()
                return _entrada_ok_payload(existente, ja_existia=True, qr_result=qr_result)
            except Exception:
                db.rollback()
                raise HTTPException(500, "Erro ao atualizar QR da entrada.")
        qr_result = apply_qr_payload_if_needed(existente, qr_payload_raw, servico_val)
        return JSONResponse(
            status_code=409,
            content={
                "code": "JA_NA_BASE",
                "message": "Este pacote já teve entrada na base.",
                "id_saida": existente.id_saida,
                "status": existente.status,
                **qr_flags_for_response(qr_result),
            },
        )

    if status_norm == "coletado":
        status_anterior = existente.status
        existente.status = STATUS_NA_BASE
        qr_result = apply_qr_payload_if_needed(existente, qr_payload_raw, servico_val)
        db.add(
            SaidaHistorico(
                id_saida=existente.id_saida,
                evento="entrada_base",
                status_anterior=status_anterior,
                status_novo=STATUS_NA_BASE,
                user_id=getattr(current_user, "id", None),
            )
        )
        try:
            db.commit()
            return _entrada_ok_payload(
                existente,
                ja_existia=True,
                promovido_coleta=True,
                qr_result=qr_result,
            )
        except Exception:
            db.rollback()
            raise HTTPException(500, "Erro ao atualizar entrada.")

    return JSONResponse(
        status_code=422,
        content={
            "code": "STATUS_INVALIDO_ENTRADA",
            "message": "Este pacote não pode receber entrada neste status.",
            "id_saida": existente.id_saida,
            "status": existente.status,
        },
    )


@router.post("/lancar-avulso", response_model=EntradaLancarAvulsoOut)
def lancar_avulso_entrada(
    payload: EntradaLancarAvulsoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cria pacotes avulso já em NA_BASE (sem seller, sem motoboy, sem cobrança)."""
    sub_base = _require_staff_entrada(db, current_user)

    quantidade = int(payload.quantidade or 0)
    if quantidade < 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "QUANTIDADE_INVALIDA", "message": "Quantidade mínima é 1."},
        )

    from saidas_routes import _PENDING_AVULSO_KEY_RE
    from upload_storage_utils import MAX_FOTOS_POR_EVENTO_TENTATIVA, build_foto_item, serialize_foto_items

    role = int(getattr(current_user, "role", 0) or 0)
    # Entrada é staff; root/admin nunca exigem. Sem motoboy no fluxo → foto opcional.
    avulso_exige_foto = False
    if role in (0, 1):
        avulso_exige_foto = False

    foto_keys: List[str] = []
    for k in list(payload.foto_object_keys or []):
        kk = (k or "").strip()
        if kk and kk not in foto_keys:
            foto_keys.append(kk)
    single_key = (payload.foto_object_key or "").strip() or None
    if single_key and single_key not in foto_keys:
        foto_keys.insert(0, single_key)
    foto_keys = foto_keys[:MAX_FOTOS_POR_EVENTO_TENTATIVA]
    for kk in foto_keys:
        if not _PENDING_AVULSO_KEY_RE.match(kk):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "FOTO_KEY_INVALIDA",
                    "message": "Foto inválida para lançamento avulso. Tire a foto novamente.",
                },
            )
    photo_ids_list: List[Optional[str]] = []
    raw_ids = list(payload.photo_ids or [])
    if payload.photo_id and not raw_ids:
        raw_ids = [payload.photo_id]
    for i, _k in enumerate(foto_keys):
        pid = (raw_ids[i] if i < len(raw_ids) else None) or None
        photo_ids_list.append((str(pid).strip() or None) if pid is not None else None)
    if avulso_exige_foto and not foto_keys:
        raise HTTPException(
            status_code=422,
            detail={"code": "FOTO_OBRIGATORIA", "message": "É necessário enviar foto ao lançar avulso."},
        )

    label_norm = _normalizar_label_avulso(payload.identificacao)
    servico = canonicalize_servico("Avulso")
    codigos: List[str] = []
    saidas_criadas: List[dict] = []
    user_id = getattr(current_user, "id", None)
    foto_payload = None
    if foto_keys:
        foto_payload = serialize_foto_items(
            [
                build_foto_item(
                    key=key,
                    evento="lancar_avulso",
                    tentativa=1,
                    photo_id=photo_ids_list[i] if i < len(photo_ids_list) else None,
                )
                for i, key in enumerate(foto_keys)
            ]
        )

    try:
        for _ in range(quantidade):
            codigo = _gerar_codigo_avulso(db, label_norm)
            row = Saida(
                sub_base=sub_base,
                username=current_user.username,
                codigo=codigo,
                servico=servico,
                status=STATUS_NA_BASE,
                base=(payload.identificacao or "").strip() or None,
            )
            db.add(row)
            db.flush()
            db.add(
                SaidaHistorico(
                    id_saida=row.id_saida,
                    evento="entrada_base",
                    status_novo=STATUS_NA_BASE,
                    user_id=user_id,
                )
            )
            if foto_payload:
                db.add(
                    SaidaDetail(
                        id_saida=row.id_saida,
                        id_entregador=0,
                        status=STATUS_NA_BASE,
                        tentativa=1,
                        foto_url=foto_payload,
                    )
                )
            codigos.append(codigo)
            saidas_criadas.append(
                {
                    "id_saida": int(row.id_saida),
                    "codigo": codigo,
                    "servico": servico,
                    "status": STATUS_NA_BASE,
                }
            )
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao lançar avulso de entrada: {e}")

    qtd = len(codigos)
    msg = (
        "1 avulso registrado na entrada."
        if qtd == 1
        else f"{qtd} avulsos registrados na entrada."
    )
    return EntradaLancarAvulsoOut(
        quantidade_criada=qtd,
        codigos=codigos,
        saidas=saidas_criadas,
        mensagem=msg,
    )
