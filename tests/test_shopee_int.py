from datetime import datetime
from unittest.mock import MagicMock, patch

from shopee_envios_pure import (
    shopee_canais_entrega,
    shopee_codigo_rastreio,
    shopee_fora_cobertura_resumo,
    shopee_janelas,
    shopee_order_destinatario,
    shopee_order_pendente_coleta,
)
from shopee_int_service import listar_pedidos_pendentes, sync_envios_sub_base


def _order(sn="250101ABCDEF", status="READY_TO_SHIP", canal="Entrega Direta", cep="01001-000", nome="Maria Silva"):
    return {
        "order_sn": sn,
        "order_status": status,
        "create_time": 1735700000,
        "shipping_carrier": canal,
        "recipient_address": {
            "name": nome,
            "phone": "5511999999999",
            "district": "Sé",
            "city": "São Paulo",
            "state": "SP",
            "zipcode": cep,
            "full_address": "Praça da Sé, 100, Sé, São Paulo, SP",
        },
        "item_list": [{"item_name": "Camisa", "model_quantity_purchased": 2}],
        "package_list": [{"package_number": "PKG1", "shipping_carrier": canal}],
    }


def test_canais_padrao_entrega_direta_e_configuravel():
    assert shopee_canais_entrega("") == ("Entrega Direta",)
    assert shopee_canais_entrega("Entrega Direta, Shopee Xpress") == ("Entrega Direta", "Shopee Xpress")


def test_pendente_coleta_filtra_status_e_canal_sem_acento():
    canais = ("Entrega Direta",)
    assert shopee_order_pendente_coleta(_order(canal="ENTREGA DIRETA - SP"), canais)
    assert shopee_order_pendente_coleta(_order(status="PROCESSED"), canais)
    assert not shopee_order_pendente_coleta(_order(status="SHIPPED"), canais)
    assert not shopee_order_pendente_coleta(_order(canal="Shopee Xpress"), canais)
    assert not shopee_order_pendente_coleta(_order(canal=None) | {"package_list": []}, canais)


def test_destinatario_mapeia_recipient_address():
    dest = shopee_order_destinatario(_order())
    assert dest["nome"] == "Maria Silva"
    assert dest["cep"] == "01001-000"
    assert dest["bairro"] == "Sé" and dest["cidade"] == "São Paulo" and dest["estado"] == "SP"
    assert dest["endereco"].startswith("Praça da Sé")
    assert dest["latitude"] is None


def test_codigo_rastreio_so_aceita_br_shopee():
    assert shopee_codigo_rastreio(" br2512345678901 ") == "BR2512345678901"
    assert shopee_codigo_rastreio("") is None
    assert shopee_codigo_rastreio(None) is None
    assert shopee_codigo_rastreio("XYZ") is None


def test_janelas_respeitam_limite_de_15_dias():
    agora = datetime(2026, 10, 13, 12, 0, 0)
    assert len(shopee_janelas(7, agora)) == 1
    janelas = shopee_janelas(30, agora)
    assert len(janelas) == 2
    assert all(fim - ini <= 15 * 24 * 3600 for ini, fim in janelas)
    assert janelas[0][1] == janelas[1][0]


@patch("shopee_int_service._shop_api_get")
def test_listar_pedidos_pendentes_filtra_canal_e_busca_rastreio(mock_api, monkeypatch):
    monkeypatch.delenv("SHOPEE_CANAIS_ENTREGA", raising=False)
    pedidos = [_order("A1"), _order("A2", canal="Shopee Xpress"), _order("A3")]

    def api(db, token, path, params):
        if path.endswith("get_order_list"):
            if params["order_status"] == "READY_TO_SHIP":
                return {"order_list": [{"order_sn": p["order_sn"]} for p in pedidos], "more": False}
            return {"order_list": [], "more": False}
        if path.endswith("get_order_detail"):
            return {"order_list": [p for p in pedidos if p["order_sn"] in params["order_sn_list"].split(",")]}
        if path.endswith("get_tracking_number"):
            return {"tracking_number": "BR2512345678901" if params["order_sn"] == "A1" else ""}
        raise AssertionError(path)

    mock_api.side_effect = api
    data = listar_pedidos_pendentes(MagicMock(), MagicMock(shop_id=99), dias=7)

    assert data["resumo"] == {"pedidos_lidos": 3, "pendentes": 2, "ignorados": 1, "sem_rastreio": 1}
    assert [e["order_sn"] for e in data["envios"]] == ["A1", "A3"]
    assert data["envios"][0]["codigo"] == "BR2512345678901"
    assert data["envios"][1]["codigo"] is None
    assert data["canais_encontrados"] == ["Entrega Direta", "Shopee Xpress"]


def _db_com_tokens(tokens, base_row):
    db = MagicMock()
    q = db.query.return_value
    q.filter.return_value = q
    q.order_by.return_value.all.return_value = tokens
    db.scalar.return_value = base_row
    db.get_bind.return_value.dialect.name = "sqlite"
    return db


def _envio(sn, codigo, cep, nome="Maria Silva"):
    return {
        "order_sn": sn,
        "codigo": codigo,
        "destinatario": shopee_order_destinatario(_order(sn, cep=cep, nome=nome)),
    }


@patch("shopee_int_service.listar_pedidos_pendentes")
def test_sync_pula_loja_sem_vinculo(mock_listar):
    db = _db_com_tokens([MagicMock(shop_id=99, shop_name="Loja", id_base=None)], None)

    out = sync_envios_sub_base(db, "BASE")

    mock_listar.assert_not_called()
    assert out["sellers"][0]["motivo"] == "sem_vinculo"


@patch("cobertura_cep_service.list_prefixos_ativos", return_value=["010"])
@patch("shopee_int_service.saidas_existentes_por_pedido", return_value={})
@patch("shopee_int_service.listar_pedidos_pendentes")
def test_sync_cria_saida_com_rastreio_e_rejeita_fora_cobertura(mock_listar, _exist, _prefixos):
    token = MagicMock(shop_id=99, shop_name="Loja", id_base=7)
    db = _db_com_tokens([token], MagicMock(base="Loja do Seller "))
    mock_listar.return_value = {
        "erros": [],
        "envios": [
            _envio("A1", "BR2512345678901", "01001-000"),
            _envio("A2", "BR2512345678902", "13000-000", nome="João"),
            _envio("A3", None, "01001-000"),
            _envio("A4", "BR2512345678904", ""),
        ],
    }

    out = sync_envios_sub_base(db, "BASE", user_id=5)

    seller = out["sellers"][0]
    assert seller["criados"] == 1 and out["criados"] == 1
    assert seller["sem_rastreio"] == 1
    assert [f["order_sn"] for f in seller["fora_cobertura"]] == ["A2", "A4"]
    assert seller["fora_cobertura"][0]["comprador"] == "João"
    adicionados = [c.args[0] for c in db.add.call_args_list]
    saida = adicionados[0]
    assert saida.codigo == "BR2512345678901" and saida.shopee_order_sn == "A1"
    assert saida.servico == "Shopee" and saida.status == "aguardando_coleta"
    assert saida.base == "Loja do Seller"
    assert adicionados[1].dest_cep == "01001-000"
    assert adicionados[2].evento == "shopee_int_sync" and adicionados[2].user_id == 5
    db.commit.assert_called_once()


@patch("cobertura_cep_service.list_prefixos_ativos", return_value=[])
@patch("shopee_int_service._completar_saida_existente", return_value=True)
@patch("shopee_int_service.listar_pedidos_pendentes")
def test_sync_nao_duplica_pedido_ja_registrado(mock_listar, _completar, _prefixos):
    token = MagicMock(shop_id=99, shop_name="Loja", id_base=7)
    db = _db_com_tokens([token], MagicMock(base="Loja"))
    mock_listar.return_value = {"erros": [], "envios": [_envio("A1", "BR2512345678901", "")]}

    with patch("shopee_int_service.saidas_existentes_por_pedido", return_value={"A1": MagicMock()}):
        out = sync_envios_sub_base(db, "BASE")

    seller = out["sellers"][0]
    assert seller["ja_registrados"] == 1 and seller["atualizados"] == 1 and seller["criados"] == 0
    assert seller["fora_cobertura"] == []
    db.add.assert_not_called()


def test_fora_cobertura_resumo():
    assert shopee_fora_cobertura_resumo(_envio("A2", "BR2512345678902", "13000-000", nome="João")) == {
        "order_sn": "A2",
        "codigo": "BR2512345678902",
        "comprador": "João",
        "cep": "13000-000",
        "cidade": "São Paulo",
        "estado": "SP",
    }
