from upload_storage_utils import (
    MAX_FOTOS_POR_EVENTO_TENTATIVA,
    build_foto_item,
    count_fotos_for_evento_tentativa,
    extract_foto_keys,
    find_foto_item,
    parse_foto_items,
    serialize_foto_items,
)


def test_parse_legacy_string_and_array():
    assert extract_foto_keys("saida/1/entregue/a.jpg") == ["saida/1/entregue/a.jpg"]
    items = parse_foto_items('["saida/1/ausente/a.jpg","saida/1/ausente/b.jpg"]')
    assert [i["key"] for i in items] == ["saida/1/ausente/a.jpg", "saida/1/ausente/b.jpg"]
    assert all(i["evento"] == "legacy" for i in items)


def test_parse_typed_objects_and_idempotency_lookup():
    payload = serialize_foto_items(
        [
            build_foto_item(key="saida/1/ausente/a.jpg", evento="ausente", tentativa=1, photo_id="p1"),
            build_foto_item(key="saida/1/entregue/b.jpg", evento="entregue", tentativa=2, photo_id="p2"),
        ]
    )
    items = parse_foto_items(payload)
    assert extract_foto_keys(payload) == ["saida/1/ausente/a.jpg", "saida/1/entregue/b.jpg"]
    assert find_foto_item(items, photo_id="p1")["key"] == "saida/1/ausente/a.jpg"
    assert find_foto_item(items, key="saida/1/entregue/b.jpg")["photo_id"] == "p2"
    assert count_fotos_for_evento_tentativa(items, "ausente", 1) == 1
    assert count_fotos_for_evento_tentativa(items, "entregue", 2) == 1
    assert MAX_FOTOS_POR_EVENTO_TENTATIVA == 3
