"""Testes de formatação de data/hora do comprovante (sem subir FastAPI)."""
from __future__ import annotations

import importlib
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo


def _load_format_dt_br():
    """Importa _format_dt_br com stubs leves das dependências de rota."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    stubs = {
        "fastapi": MagicMock(),
        "fastapi.responses": MagicMock(),
        "pydantic": MagicMock(),
        "sqlalchemy": MagicMock(),
        "sqlalchemy.orm": MagicMock(),
        "auth": MagicMock(),
        "db": MagicMock(),
        "models": MagicMock(),
        "upload_storage_utils": MagicMock(
            B2_BUCKET_NAME="x",
            extract_foto_keys=MagicMock(),
            generate_presigned_get_url=MagicMock(),
            get_object_bytes=MagicMock(),
            parse_id_saida_from_object_key=MagicMock(),
        ),
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    try:
        for name, mod in stubs.items():
            sys.modules[name] = mod
        # Field usado em type hints / BaseModel — pydantic stub precisa de Field
        stubs["pydantic"].Field = MagicMock()
        stubs["pydantic"].BaseModel = type("BaseModel", (), {})
        if "upload_routes" in sys.modules:
            del sys.modules["upload_routes"]
        upload_routes = importlib.import_module("upload_routes")
        return upload_routes._format_dt_br
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


class TestFormatDtBrComprovante(unittest.TestCase):
    format_dt_br = staticmethod(_load_format_dt_br())

    def test_naive_historico_nao_converte_utc(self):
        """Timeline e histórico usam horário de parede; comprovante deve bater."""
        dt = datetime(2026, 8, 4, 20, 30, 0)
        self.assertEqual(self.format_dt_br(dt), "04/08/2026 às 20:30")
        self.assertNotEqual(self.format_dt_br(dt), "04/08/2026 às 17:30")

    def test_legacy_data_hora_entrega_utc(self):
        """Fallback data_hora_entrega (utcnow) precisa converter UTC → BRT."""
        dt = datetime(2026, 8, 4, 23, 30, 0)  # 20:30 BRT em UTC
        self.assertEqual(
            self.format_dt_br(dt, assume_utc=True),
            "04/08/2026 às 20:30",
        )

    def test_aware_converte_para_operacao_tz(self):
        dt = datetime(2026, 8, 4, 23, 30, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(self.format_dt_br(dt), "04/08/2026 às 20:30")


if __name__ == "__main__":
    unittest.main()
