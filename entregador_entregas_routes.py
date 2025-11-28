# entregador_entregas_routes.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import Optional, List

from db import get_db
from auth import get_current_user
from models import Saida, Entregador, SaidaDetail, User

router = APIRouter(prefix="/entregador/entregas", tags=["Entregador - Entregas"])


# ============================================================
# HELPERS
# ============================================================

def _get_entregador_logado(db: Session, current_user: User) -> Entregador:
    """Retorna o entregador vinculado ao user logado."""
    # tenta por username_entregador
    ue = getattr(current_user, "username_entregador", None)
    if ue:
        ent = db.scalars(select(Entregador).where(Entregador.username_entregador == ue)).first()
        if ent:
            return ent

    # tenta por username normal
    un = getattr(current_user, "username", None)
    if un:
        ent = db.scalars(select(Entregador).where(Entregador.username_entregador == un)).first()
        if ent:
            return ent

    raise HTTPException(401, "Usuário logado não é um entregador válido.")


def _get_saida_by_codigo(db: Session, codigo: str, sub_base: str) -> Optional[Saida]:
    return db.scalars(
        select(Saida).where(Saida.sub_base == sub_base, Saida.codigo == codigo)
    ).first()


def _get_detail(db: Session, id_saida: int) -> Optional[SaidaDetail]:
    return db.scalars(
        select(SaidaDetail).where(SaidaDetail.id_saida == id_saida)
    ).first()

    
# ============================================================
# 0) LOGIN SIMPLES DO ENTREGADOR (sem senha)
# ============================================================
@router.post("/login-simples")
def login_simples(payload: dict,
                  db: Session = Depends(get_db)):

    telefone = (payload.get("telefone") or "").strip()

    if not telefone:
        raise HTTPException(422, "Telefone é obrigatório.")

    # buscar entregador pelo telefone
    entregador = db.scalars(
        select(Entregador).where(Entregador.telefone == telefone)
    ).first()

    if not entregador:
        raise HTTPException(404, "Entregador não encontrado.")

    if not entregador.ativo:
        raise HTTPException(403, "Entregador inativo.")

    # buscar usuário vinculado
    user = db.scalars(
        select(User).where(User.username_entregador == entregador.username_entregador)
    ).first()

    if not user:
        raise HTTPException(404, "Usuário vinculado ao entregador não encontrado.")

    # gerar token
    from auth import create_access_token
    token = create_access_token({"sub": user.username})

    return {
        "ok": True,
        "token": token,
        "entregador": {
            "id": entregador.id_entregador,
            "nome": entregador.nome,
            "telefone": entregador.telefone,
            "sub_base": entregador.sub_base
        }
    }


# ============================================================
# 1) SCAN
# ============================================================
class ScanPayload:
    codigo: str


@router.post("/scan")
def scan_codigo(payload: dict, 
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """
    Entregador bipou um código.
    Regras:

    - Se não existe → erro.
    - Se existe e está sem entregador/Coletado → atribui ao entregador logado.
    - Se está com ele → OK (já é dele).
    - Se está com outro → retorna info para confirmar "assumir".
    """
    codigo = (payload.get("codigo") or "").strip()
    if not codigo:
        raise HTTPException(422, "Código inválido.")

    ent = _get_entregador_logado(db, current_user)
    sub_base = ent.sub_base

    saida = _get_saida_by_codigo(db, codigo, sub_base)
    if not saida:
        raise HTTPException(404, "Código não encontrado na sua sub_base.")

    # Caso 1 — saída sem entregador ou coletado
    if not saida.entregador or saida.status.lower() in ("coletado", "coletada"):
        saida.entregador = ent.username_entregador
        saida.status = "Saiu para entrega"
        db.commit()

        # cria detail se não existir
        detail = _get_detail(db, saida.id_saida)
        if not detail:
            detail = SaidaDetail(
                id_saida=saida.id_saida,
                entregador=ent.username_entregador,
                status="Em Rota",
                tentativa=1
            )
            db.add(detail)
            db.commit()

        return {
            "ok": True,
            "action": "atribuido",
            "id_saida": saida.id_saida,
            "mensagem": "Entrega atribuída a você."
        }

    # Caso 2 — já está com ele
    if saida.entregador == ent.username_entregador:
        return {
            "ok": True,
            "action": "ja_eh_seu",
            "id_saida": saida.id_saida
        }

    # Caso 3 — está com outro entregador
    return {
        "ok": False,
        "action": "outro_entregador",
        "atual": saida.entregador,
        "id_saida": saida.id_saida,
        "mensagem": f"Pedido está com {saida.entregador}. Deseja assumir?"
    }


# ============================================================
# 2) ASSUMIR ENTREGA DE OUTRO ENTREGADOR
# ============================================================
@router.post("/assumir")
def assumir_entrega(payload: dict,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    id_saida = payload.get("id_saida")
    if not id_saida:
        raise HTTPException(422, "id_saida é obrigatório.")

    ent = _get_entregador_logado(db, current_user)
    saida = db.get(Saida, id_saida)

    if not saida or saida.sub_base != ent.sub_base:
        raise HTTPException(404, "Saída não encontrada.")

    # reatribuindo
    saida.entregador = ent.username_entregador
    saida.status = "Saiu para entrega"
    db.commit()

    # atualiza detail
    detail = _get_detail(db, saida.id_saida)
    if not detail:
        detail = SaidaDetail(
            id_saida=saida.id_saida,
            entregador=ent.username_entregador,
            status="Em Rota",
            tentativa=1
        )
        db.add(detail)
    else:
        detail.entregador = ent.username_entregador
        detail.status = "Em Rota"

    db.commit()

    return {"ok": True, "mensagem": "Pedido assumido com sucesso.", "id_saida": id_saida}


# ============================================================
# 3) LISTAR PENDENTES
# ============================================================
@router.get("/pendentes")
def listar_pendentes(db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    ent = _get_entregador_logado(db, current_user)

    rows = db.scalars(
        select(Saida)
        .where(
            Saida.sub_base == ent.sub_base,
            Saida.entregador == ent.username_entregador,
            Saida.status == "Saiu para entrega"
        )
        .order_by(Saida.timestamp.desc())
    ).all()

    return [{"id_saida": r.id_saida, "codigo": r.codigo, "status": r.status} for r in rows]


# ============================================================
# 4) LISTAR OCORRÊNCIAS
# ============================================================
@router.get("/ocorrencias")
def listar_ocorrencias(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    ent = _get_entregador_logado(db, current_user)

    rows = db.scalars(
        select(SaidaDetail)
        .where(
            SaidaDetail.entregador == ent.username_entregador,
            SaidaDetail.status == "Ocorrência"
        )
        .order_by(SaidaDetail.timestamp.desc())
    ).all()

    return rows


# ============================================================
# 5) LISTAR FINALIZADAS
# ============================================================
@router.get("/finalizadas")
def listar_finalizadas(db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    ent = _get_entregador_logado(db, current_user)

    rows = db.scalars(
        select(SaidaDetail)
        .where(
            SaidaDetail.entregador == ent.username_entregador,
            SaidaDetail.status.in_(["Entregue", "Cancelado"])
        )
        .order_by(SaidaDetail.timestamp.desc())
    ).all()

    return rows


# ============================================================
# 6) REGISTRAR ENTREGA
# ============================================================
@router.post("/entregar")
def registrar_entrega(payload: dict,
                      db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    id_saida = payload.get("id_saida")
    if not id_saida:
        raise HTTPException(422, "id_saida é obrigatório.")

    ent = _get_entregador_logado(db, current_user)

    saida = db.get(Saida, id_saida)
    if not saida or saida.sub_base != ent.sub_base:
        raise HTTPException(404, "Saída não encontrada.")

    # Atualiza Saida
    saida.status = "Entregue"

    # Atualiza detalhe
    detail = _get_detail(db, id_saida)
    if not detail:
        detail = SaidaDetail(id_saida=id_saida, entregador=ent.username_entregador)

    detail.status = "Entregue"
    detail.tipo_recebedor = payload.get("tipo_recebedor")
    detail.nome_recebedor = payload.get("nome_recebedor")
    detail.tipo_documento = payload.get("tipo_documento")
    detail.numero_documento = payload.get("numero_documento")
    detail.observacao_entrega = payload.get("observacao")
    detail.foto_url = payload.get("foto_url")

    db.add(detail)
    db.commit()

    return {"ok": True, "mensagem": "Entrega registrada."}


# ============================================================
# 7) REGISTRAR OCORRÊNCIA
# ============================================================
@router.post("/ocorrencia")
def registrar_ocorrencia(payload: dict,
                         db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    id_saida = payload.get("id_saida")
    motivo = payload.get("motivo")
    foto_url = payload.get("foto_url")
    obs = payload.get("observacao")

    if not id_saida or not motivo:
        raise HTTPException(422, "Campos obrigatórios: id_saida, motivo")

    ent = _get_entregador_logado(db, current_user)
    saida = db.get(Saida, id_saida)

    if not saida or saida.sub_base != ent.sub_base:
        raise HTTPException(404, "Saída não encontrada.")

    # atualizar STATUS geral
    saida.status = "Ocorrência"

    detail = _get_detail(db, id_saida)
    if not detail:
        detail = SaidaDetail(id_saida=id_saida, entregador=ent.username_entregador)

    # incrementa tentativa
    detail.tentativa = (detail.tentativa or 1) + 1
    detail.status = "Ocorrência"
    detail.motivo_ocorrencia = motivo
    detail.observacao_ocorrencia = obs
    detail.foto_url = foto_url

    # Cancelamento automático
    if detail.tentativa >= 3 or motivo.lower() == "recusado":
        detail.status = "Cancelado"
        saida.status = "Cancelado"

    db.add(detail)
    db.commit()

    return {"ok": True, "mensagem": "Ocorrência registrada."}


# ============================================================
# 8) NOVA TENTATIVA
# ============================================================
@router.post("/tentativa")
def registrar_tentativa(payload: dict,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    id_saida = payload.get("id_saida")
    if not id_saida:
        raise HTTPException(422, "id_saida é obrigatório.")

    ent = _get_entregador_logado(db, current_user)
    saida = db.get(Saida, id_saida)

    if not saida or saida.sub_base != ent.sub_base:
        raise HTTPException(404, "Saída não encontrada.")

    saida.status = "Em Tentativa"

    detail = _get_detail(db, id_saida)
    if not detail:
        detail = SaidaDetail(
            id_saida=id_saida,
            entregador=ent.username_entregador,
            status="Em Rota",
            tentativa=1
        )
    else:
        detail.tentativa = (detail.tentativa or 1) + 1
        detail.status = "Em Rota"

    db.add(detail)
    db.commit()

    return {"ok": True, "mensagem": "Nova tentativa registrada."}


# ============================================================
# 9) DETALHE DA ENTREGA
# ============================================================
@router.get("/detalhe/{id_saida}")
def detalhe_entrega(id_saida: int,
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    ent = _get_entregador_logado(db, current_user)

    saida = db.get(Saida, id_saida)
    if not saida or saida.sub_base != ent.sub_base:
        raise HTTPException(404, "Saída não encontrada.")

    detail = _get_detail(db, id_saida)

    return {
        "saida": {
            "id_saida": saida.id_saida,
            "codigo": saida.codigo,
            "status": saida.status,
            "entregador": saida.entregador,
        },
        "detail": detail
    }
