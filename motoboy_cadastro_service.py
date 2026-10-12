"""Criação de motoboy (User role=4 + Motoboy + MotoboySubBase) compartilhada.

Usado por POST /users (admin) e pelo autocadastro por link de convite (PRD-004).
Nenhuma função aqui faz commit: quem chama controla a transação.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from models import Motoboy, MotoboySubBase, Owner, User
from name_normalizer import normalize_person_name

MOTOBOY_ROLE = 4

PERMISSOES_MOTOBOY = (
    "pode_ler_coleta",
    "pode_realizar_coleta",
    "pode_ler_saida",
    "pode_digitar_codigo_manual",
    "pode_lancar_avulso",
    "pode_criar_avulso_coleta",
    "pode_criar_avulso_saida",
    "avulso_exige_foto",
)

_CAMPOS_MOTOBOY = (
    "documento",
    "cnpj",
    "chave_pix",
    "rua",
    "numero",
    "complemento",
    "bairro",
    "cidade",
    "estado",
    "cep",
)


def resolver_permissoes_motoboy(owner: Owner, overrides: Optional[Mapping[str, Any]] = None) -> dict:
    """Defaults oficiais do Owner (Políticas gerais); overrides não-None sobrescrevem."""
    o = dict(overrides or {})

    def ov(key: str):
        return o.get(key)

    def_coleta = bool(getattr(owner, "default_pode_realizar_coleta", False))
    def_saida = bool(getattr(owner, "default_pode_ler_saida", True))
    def_digitar = bool(getattr(owner, "default_pode_digitar_codigo_manual", False))
    def_avulso = bool(getattr(owner, "default_pode_lancar_avulso", True))
    def_avulso_coleta = bool(getattr(owner, "default_pode_criar_avulso_coleta", def_avulso))
    def_avulso_saida = bool(getattr(owner, "default_pode_criar_avulso_saida", def_avulso))
    def_foto = bool(getattr(owner, "default_avulso_exige_foto", True))

    pode_ler_coleta = ov("pode_ler_coleta") if ov("pode_ler_coleta") is not None else def_coleta
    pode_realizar_coleta = (
        ov("pode_realizar_coleta")
        if ov("pode_realizar_coleta") is not None
        else (pode_ler_coleta if ov("pode_ler_coleta") is not None else def_coleta)
    )
    pode_ler_saida = ov("pode_ler_saida") if ov("pode_ler_saida") is not None else def_saida
    pode_digitar_codigo_manual = (
        ov("pode_digitar_codigo_manual") if ov("pode_digitar_codigo_manual") is not None else def_digitar
    )
    pode_criar_avulso_coleta = (
        ov("pode_criar_avulso_coleta")
        if ov("pode_criar_avulso_coleta") is not None
        else (ov("pode_lancar_avulso") if ov("pode_lancar_avulso") is not None else def_avulso_coleta)
    )
    pode_criar_avulso_saida = (
        ov("pode_criar_avulso_saida")
        if ov("pode_criar_avulso_saida") is not None
        else (ov("pode_lancar_avulso") if ov("pode_lancar_avulso") is not None else def_avulso_saida)
    )
    pode_lancar_avulso = bool(pode_criar_avulso_coleta or pode_criar_avulso_saida)
    avulso_exige_foto = bool(ov("avulso_exige_foto")) if ov("avulso_exige_foto") is not None else def_foto
    if not pode_lancar_avulso:
        avulso_exige_foto = False
    if getattr(owner, "ignorar_coleta", False):
        pode_ler_coleta = False
        pode_realizar_coleta = False

    return {
        "pode_ler_coleta": pode_ler_coleta,
        "pode_realizar_coleta": pode_realizar_coleta,
        "pode_ler_saida": pode_ler_saida,
        "pode_digitar_codigo_manual": pode_digitar_codigo_manual,
        "pode_lancar_avulso": pode_lancar_avulso,
        "pode_criar_avulso_coleta": bool(pode_criar_avulso_coleta),
        "pode_criar_avulso_saida": bool(pode_criar_avulso_saida),
        "avulso_exige_foto": avulso_exige_foto,
    }


def adicionar_registro_motoboy(
    db: Session,
    *,
    user: User,
    owner: Owner,
    sub_base: str,
    dados: Mapping[str, Any],
    overrides: Optional[Mapping[str, Any]] = None,
) -> Motoboy:
    """Cria Motoboy + MotoboySubBase para um User já persistido (flush feito)."""
    d = {k: dados.get(k) for k in _CAMPOS_MOTOBOY}
    motoboy = Motoboy(
        user_id=user.id,
        sub_base=sub_base,
        documento=(d["documento"] or "").strip(),
        cnpj=(d["cnpj"] or "").strip(),
        chave_pix=(d["chave_pix"] or "").strip() or None,
        rua=(d["rua"] or "").strip(),
        numero=(d["numero"] or "").strip(),
        complemento=(d["complemento"] or "").strip() or None,
        bairro=(d["bairro"] or "").strip(),
        cidade=(d["cidade"] or "").strip(),
        estado=(d["estado"] or "").strip() or None,
        cep=(d["cep"] or "").strip(),
        ativo=True,
        data_cadastro=date.today(),
        claims_version=0,
        **resolver_permissoes_motoboy(owner, overrides),
    )
    db.add(motoboy)
    db.flush()

    db.add(MotoboySubBase(motoboy_id=motoboy.id_motoboy, sub_base=sub_base, ativo=True))
    return motoboy


def criar_motoboy(
    db: Session,
    *,
    owner: Owner,
    sub_base: str,
    username: str,
    password_hash: str,
    contato: str,
    nome: Optional[str],
    sobrenome: Optional[str],
    email: Optional[str],
    data_nascimento: Optional[date],
    must_change_password: bool,
    dados: Mapping[str, Any],
    overrides: Optional[Mapping[str, Any]] = None,
) -> User:
    """Cria User (role=4, ativo) + Motoboy + MotoboySubBase. Não faz commit."""
    user = User(
        email=email,
        password_hash=password_hash,
        username=username,
        contato=contato,
        nome=normalize_person_name(nome),
        sobrenome=normalize_person_name(sobrenome),
        data_nascimento=data_nascimento,
        status=True,
        role=MOTOBOY_ROLE,
        coletador=False,
        sub_base=sub_base,
        must_change_password=must_change_password,
    )
    db.add(user)
    db.flush()
    adicionar_registro_motoboy(db, user=user, owner=owner, sub_base=sub_base, dados=dados, overrides=overrides)
    return user
