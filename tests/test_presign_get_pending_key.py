"""Contrato: keys pending de avulso não têm id_saida no path (presign-get precisa tratar)."""
import re

_OBJECT_KEY_SAIDA_RE = re.compile(r"^saida/(\d+)/")


def test_pending_avulso_key_has_no_numeric_saida_id():
    key = "saida/pending/lancar_avulso/deadbeefcafebabe.jpg"
    assert _OBJECT_KEY_SAIDA_RE.match(key) is None
    assert key.startswith("saida/pending/")


def test_normal_saida_key_parses_id():
    m = _OBJECT_KEY_SAIDA_RE.match("saida/42/entregue/deadbeef.jpg")
    assert m is not None
    assert int(m.group(1)) == 42
