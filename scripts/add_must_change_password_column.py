from __future__ import annotations

"""
Pequena migração ad-hoc para adicionar a coluna must_change_password em users.

Uso (a partir da raiz do projeto):

  python scripts/add_must_change_password_column.py

Apenas executa um ALTER TABLE simples. Execute uma única vez em cada ambiente.
"""

from sqlalchemy import text

from db import engine


def main() -> None:
    ddl = """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT TRUE;
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


if __name__ == "__main__":
    main()

