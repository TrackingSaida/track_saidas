"""Cartão do comprovante compartilhado, sem subir FastAPI."""
from __future__ import annotations

import importlib
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image


def _load_builder():
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
        "upload_storage_utils": MagicMock(B2_BUCKET_NAME="x"),
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    try:
        for name, mod in stubs.items():
            sys.modules[name] = mod
        stubs["pydantic"].Field = MagicMock()
        stubs["pydantic"].BaseModel = type("BaseModel", (), {})
        sys.modules.pop("upload_routes", None)
        return importlib.import_module("upload_routes")._build_comprovante_share_image_bytes
    finally:
        for name, prev in saved.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (800, 1400)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class TestComprovanteShareImage(unittest.TestCase):
    build = staticmethod(_load_builder())

    def _resumo(self, **extra):
        base = {
            "status": "Entregue",
            "codigo": "48486491814",
            "data_hora": "26/11/2026 às 18:16",
            "entregador": "Entregador",
            "nome_recebedor": None,
            "motivo": None,
            "observacao": None,
        }
        base.update(extra)
        return base

    def test_quadrado_do_topo_mostra_o_inicio_da_foto(self):
        raw = self.build(
            photo_bytes_list=[_jpeg((210, 40, 40))],
            resumo=self._resumo(),
        )
        image = Image.open(BytesIO(raw))
        self.assertEqual(image.format, "JPEG")
        self.assertGreaterEqual(image.width, 720)
        self.assertLessEqual(image.width, 1080)
        self.assertGreater(image.height, image.width)

        paper = image.getpixel((8, 4))
        self.assertGreater(paper[0], 245)
        self.assertGreater(paper[1], 245)
        self.assertGreater(paper[2], 245)

        photo = image.getpixel((image.width // 2, image.width - 12))
        self.assertGreater(photo[0], 160)
        self.assertLess(photo[1], 90)
        self.assertLess(photo[2], 90)

    def test_tres_fotos_ficam_em_4_por_3(self):
        raw = self.build(
            photo_bytes_list=[
                _jpeg((20, 20, 20), (900, 1600)),
                _jpeg((20, 20, 20), (900, 1600)),
                _jpeg((20, 20, 20), (900, 1600)),
            ],
            resumo=self._resumo(),
        )
        image = Image.open(BytesIO(raw))
        photo_h = int(round(image.width * 3 / 4))
        self.assertGreater(image.height, photo_h * 3)

    def test_ocorrencia_nao_pinta_faixa_no_topo(self):
        raw = self.build(
            photo_bytes_list=[_jpeg((40, 40, 210))],
            resumo=self._resumo(
                status="Ocorrência",
                motivo="Cliente ausente",
                entregador="Ana",
            ),
        )
        image = Image.open(BytesIO(raw))
        top = image.getpixel((4, 4))
        self.assertGreater(top[0], 245)
        self.assertGreater(top[1], 245)
        self.assertGreater(top[2], 245)
        photo = image.getpixel((image.width // 2, image.width - 12))
        self.assertGreater(photo[2], 160)
        self.assertLess(photo[0], 90)


if __name__ == "__main__":
    unittest.main()
