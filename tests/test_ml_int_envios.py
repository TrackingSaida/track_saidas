import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from datetime import datetime
from unittest.mock import MagicMock, patch

from ml_int_envios_pure import (
    destinatario_to_saida_detail_fields,
    ml_date_from,
    ml_order_shipment_id,
    ml_shipment_destinatario,
    ml_shipment_pendente_coleta,
)
from ml_int_service import listar_envios_flex_pendentes


def _ship_new_format(**kwargs):
    ship = {
        "id": 45123456789,
        "status": "ready_to_ship",
        "substatus": "printed",
        "logistic": {"mode": "me2", "type": "self_service", "direction": "forward"},
        "tracking_number": "TRK1",
        "destination": {
            "receiver_name": "Maria Silva",
            "receiver_phone": "11999990000",
            "shipping_address": {
                "address_line": "Rua das Flores 123",
                "street_name": "Rua das Flores",
                "street_number": "123",
                "comment": "Apto 4",
                "zip_code": "01001000",
                "city": {"id": "BR-SP-44", "name": "São Paulo"},
                "state": {"id": "BR-SP", "name": "São Paulo"},
                "neighborhood": {"id": None, "name": "Centro"},
                "latitude": -23.55,
                "longitude": -46.63,
            },
        },
    }
    ship.update(kwargs)
    return ship


def test_destinatario_formato_novo():
    dest = ml_shipment_destinatario(_ship_new_format())
    assert dest["nome"] == "Maria Silva"
    assert dest["telefone"] == "11999990000"
    assert dest["rua"] == "Rua das Flores"
    assert dest["numero"] == "123"
    assert dest["complemento"] == "Apto 4"
    assert dest["bairro"] == "Centro"
    assert dest["cidade"] == "São Paulo"
    assert dest["estado"] == "São Paulo"
    assert dest["cep"] == "01001000"
    assert dest["latitude"] == -23.55


def test_destinatario_formato_antigo_receiver_address():
    ship = {
        "status": "ready_to_ship",
        "logistic_type": "self_service",
        "receiver_address": {
            "street_name": "Av. Brasil",
            "street_number": 500,
            "zip_code": "20000000",
            "city": {"name": "Rio de Janeiro"},
            "state": {"name": "Rio de Janeiro"},
            "neighborhood": {"name": "Centro"},
            "receiver_name": "João",
            "receiver_phone": None,
            "latitude": 0,
            "longitude": 0,
        },
    }
    dest = ml_shipment_destinatario(ship)
    assert dest["nome"] == "João"
    assert dest["numero"] == "500"
    assert dest["cidade"] == "Rio de Janeiro"
    assert dest["telefone"] is None
    assert dest["latitude"] is None
    assert ml_shipment_pendente_coleta(ship) is True


def test_pendente_coleta_somente_flex_ready_to_ship():
    assert ml_shipment_pendente_coleta(_ship_new_format()) is True
    assert ml_shipment_pendente_coleta(_ship_new_format(status="shipped")) is False
    assert ml_shipment_pendente_coleta(_ship_new_format(status="delivered")) is False
    assert ml_shipment_pendente_coleta(_ship_new_format(logistic={"type": "cross_docking"})) is False


def test_saida_detail_fields_sem_coordenadas_omite_lat_lng():
    fields = destinatario_to_saida_detail_fields({"nome": "Ana", "cidade": "X", "latitude": None, "longitude": None})
    assert fields == {"dest_nome": "Ana", "dest_cidade": "X"}

    com_coord = destinatario_to_saida_detail_fields(ml_shipment_destinatario(_ship_new_format()))
    assert com_coord["latitude"] == -23.55
    assert com_coord["endereco_origem"] == "mercado_livre"
    assert com_coord["dest_contato"] == "11999990000"


def test_order_shipment_id_e_date_from():
    assert ml_order_shipment_id({"id": 1, "shipping": {"id": 45123456789}}) == 45123456789
    assert ml_order_shipment_id({"id": 1, "shipping": {}}) is None
    assert ml_order_shipment_id(123) is None
    assert ml_date_from(7, now=datetime(2026, 10, 13, 15, 0)) == "2026-10-06T00:00:00.000-00:00"


@patch("ml_int_service.fetch_shipment")
@patch("ml_int_service.fetch_orders_search")
@patch("ml_int_service.get_valid_access_token", return_value="tok")
def test_listar_pendentes_agrupa_pack_e_filtra(_tok, mock_search, mock_ship):
    mock_search.return_value = {
        "paging": {"total": 3},
        "results": [
            {"id": 1, "pack_id": 9, "shipping": {"id": 45123456789}, "date_created": "d1",
             "order_items": [{"item": {"title": "Camisa"}, "quantity": 2}]},
            {"id": 2, "pack_id": 9, "shipping": {"id": 45123456789}, "order_items": []},
            {"id": 3, "shipping": {"id": 45999999999}, "order_items": []},
        ],
    }

    def ship_side(token, sid, **kw):
        if sid == 45999999999:
            return _ship_new_format(id=sid, status="shipped")
        return _ship_new_format(id=sid)

    mock_ship.side_effect = ship_side

    data = listar_envios_flex_pendentes(MagicMock(), 111, "BASE", dias=7)

    assert mock_search.call_count == 1
    kwargs = mock_search.call_args.kwargs
    assert kwargs["order_status"] == "paid"
    assert kwargs["sort"] == "date_desc"
    assert data["resumo"] == {"pedidos_lidos": 3, "envios_consultados": 2, "pendentes": 1, "ignorados": 1}
    envio = data["envios"][0]
    assert envio["codigo"] == "45123456789"
    assert envio["order_ids"] == [1, 2]
    assert envio["itens"] == [{"titulo": "Camisa", "quantidade": 2}]
    assert envio["destinatario"]["nome"] == "Maria Silva"
