#!/usr/bin/env python3
"""
Script de migração: Entregador -> User + Motoboy

Migra registros da tabela legada `entregador` para `users` (role=4) e `motoboys`,
e atualiza `saidas.motoboy_id` onde `saidas.entregador_id` aponta para o entregador migrado.

Uso:
  cd track_saidas
  python scripts/migrate_entregador_to_user_motoboy.py [--dry-run] [--senha-padrao SENHA]

  --dry-run        Apenas simula, não grava no banco
  --senha-padrao   Senha para os usuários migrados (default: migrado_trocar_senha)

Idempotente: entregadores já migrados (User com email entregador_{id}@migrado.local)
são ignorados e apenas as saídas são atualizadas se necessário.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Garante que o projeto está no path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import SessionLocal
from models import Entregador, User, Motoboy, MotoboySubBase, Saida
from auth import get_password_hash


def _normalizar_nome(s: str) -> str:
    """Lower + remove acentos para comparação."""
    s = (s or "").strip().lower()
    s = re.sub(r"[àáâãäå]", "a", s)
    s = re.sub(r"[èéêë]", "e", s)
    s = re.sub(r"[ìíîï]", "i", s)
    s = re.sub(r"[òóôõö]", "o", s)
    s = re.sub(r"[ùúûü]", "u", s)
    s = re.sub(r"[ç]", "c", s)
    return s


def migrate(db: Session, dry_run: bool = False, senha_padrao: str = "migrado_trocar_senha") -> dict:
    """
    Migra entregadores para User + Motoboy e atualiza saidas.
    Retorna estatísticas: { criados_user, criados_motoboy, saidas_atualizadas, pulados, erros }
    """
    stats = {"criados_user": 0, "criados_motoboy": 0, "saidas_atualizadas": 0, "pulados": 0, "erros": []}

    entregadores = db.execute(select(Entregador).order_by(Entregador.id_entregador)).scalars().all()
    id_entregador_to_motoboy_id: dict[int, int] = {}
    nome_to_motoboy_id: dict[tuple[str, str], int] = {}  # (sub_base, nome_norm) -> motoboy_id

    for ent in entregadores:
        email_canon = f"entregador_{ent.id_entregador}@migrado.local"

        # Já migrado?
        existing = db.scalar(select(User).where(User.email == email_canon))
        if existing:
            motoboy = db.scalar(select(Motoboy).where(Motoboy.user_id == existing.id))
            if motoboy:
                id_entregador_to_motoboy_id[ent.id_entregador] = motoboy.id_motoboy
                sub = (ent.sub_base or "").strip()
                nome_norm = _normalizar_nome(ent.nome or "")
                if sub and nome_norm:
                    nome_to_motoboy_id[(sub, nome_norm)] = motoboy.id_motoboy
                stats["pulados"] += 1
                continue

        try:
            if dry_run:
                stats["criados_user"] += 1
                stats["criados_motoboy"] += 1
                # Simula um motoboy_id fictício para o mapeamento (não usado no update real)
                id_entregador_to_motoboy_id[ent.id_entregador] = -1
                continue

            user = User(
                email=email_canon,
                password_hash=get_password_hash(senha_padrao),
                username=(ent.username_entregador or ent.nome or f"entregador_{ent.id_entregador}").strip()[:100],
                contato=(ent.telefone or "0000000000").strip()[:50],
                nome=(ent.nome or "").strip()[:100] or None,
                sobrenome=None,
                status=bool(ent.ativo),
                sub_base=ent.sub_base,
                coletador=bool(ent.coletador),
                username_entregador=ent.username_entregador,
                role=4,
            )
            db.add(user)
            db.flush()

            motoboy = Motoboy(
                user_id=user.id,
                sub_base=ent.sub_base,
                documento=ent.documento,
                rua=(ent.rua or "").strip(),
                numero=(ent.numero or "").strip(),
                complemento=(ent.complemento or "").strip() or None,
                bairro=(ent.bairro or "").strip(),
                cidade=(ent.cidade or "").strip(),
                estado=None,
                cep=(ent.cep or "00000000").strip(),
                ativo=bool(ent.ativo),
                data_cadastro=ent.data_cadastro,
                pode_ler_coleta=bool(ent.coletador),
                pode_ler_saida=True,
            )
            db.add(motoboy)
            db.flush()

            sub_base_val = (ent.sub_base or "").strip()
            if sub_base_val:
                msb = MotoboySubBase(motoboy_id=motoboy.id_motoboy, sub_base=sub_base_val, ativo=True)
                db.add(msb)

            id_entregador_to_motoboy_id[ent.id_entregador] = motoboy.id_motoboy
            if sub_base_val and (ent.nome or "").strip():
                nome_to_motoboy_id[(sub_base_val, _normalizar_nome(ent.nome))] = motoboy.id_motoboy

            stats["criados_user"] += 1
            stats["criados_motoboy"] += 1

        except Exception as e:
            stats["erros"].append(f"Entregador {ent.id_entregador} ({ent.nome}): {e}")
            if not dry_run:
                db.rollback()
                raise

    # Atualizar Saida.motoboy_id onde entregador_id está no mapeamento
    for ent_id, motoboy_id in id_entregador_to_motoboy_id.items():
        if motoboy_id <= 0:
            continue
        stmt = select(Saida).where(
            Saida.entregador_id == ent_id,
            (Saida.motoboy_id.is_(None)) | (Saida.motoboy_id != motoboy_id),
        )
        rows = db.execute(stmt).scalars().all()
        for s in rows:
            if not dry_run:
                s.motoboy_id = motoboy_id
            stats["saidas_atualizadas"] += 1

    # Opcional: Saidas com apenas entregador (texto) e sem entregador_id
    stmt_sem_id = select(Saida).where(
        Saida.entregador_id.is_(None),
        Saida.motoboy_id.is_(None),
        Saida.entregador.isnot(None),
        Saida.entregador != "",
    )
    for s in db.execute(stmt_sem_id).scalars().all():
        sub = (s.sub_base or "").strip()
        nome_raw = (s.entregador or "").strip()
        nome_norm = _normalizar_nome(nome_raw)
        if not sub or not nome_norm:
            continue
        motoboy_id = nome_to_motoboy_id.get((sub, nome_norm))
        if motoboy_id:
            if not dry_run:
                s.motoboy_id = motoboy_id
            stats["saidas_atualizadas"] += 1

    if not dry_run:
        db.commit()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Migra entregadores para User + Motoboy")
    parser.add_argument("--dry-run", action="store_true", help="Simula sem gravar")
    parser.add_argument("--senha-padrao", default="migrado_trocar_senha", help="Senha para usuários migrados")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stats = migrate(db, dry_run=args.dry_run, senha_padrao=args.senha_padrao)
        mode = "[DRY-RUN] " if args.dry_run else ""
        print(f"{mode}Migração concluída:")
        print(f"  Users criados:     {stats['criados_user']}")
        print(f"  Motoboys criados:  {stats['criados_motoboy']}")
        print(f"  Saídas atualizadas:{stats['saidas_atualizadas']}")
        print(f"  Entregadores já migrados (pulados): {stats['pulados']}")
        if stats["erros"]:
            print("  Erros:")
            for e in stats["erros"]:
                print(f"    - {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
