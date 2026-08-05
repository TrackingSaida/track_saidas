"""Validação de object_key pending no lançamento avulso."""
import re

_PENDING_AVULSO_KEY_RE = re.compile(
    r"^saida/pending/lancar_avulso/[a-fA-F0-9]{16,64}\.(jpg|jpeg|png|gif|webp)$"
)


def test_pending_avulso_key_accepts_presign_format():
    key = "saida/pending/lancar_avulso/" + ("ab" * 16) + ".jpg"
    assert _PENDING_AVULSO_KEY_RE.match(key)


def test_pending_avulso_key_rejects_other_prefixes():
    assert _PENDING_AVULSO_KEY_RE.match("saida/12/lancar_avulso/abc.jpg") is None
    assert _PENDING_AVULSO_KEY_RE.match("saida/pending/entregue/" + ("ab" * 16) + ".jpg") is None
    assert _PENDING_AVULSO_KEY_RE.match("https://example.com/x.jpg") is None
