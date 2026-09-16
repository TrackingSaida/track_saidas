from http_error_public import CLIENT_SAFE_500, looks_like_internal_error_dump, public_error_body


HOMOLOG_SQL_DUMP = (
    "(psycopg2.errors.UndefinedColumn) column motoboys_1.claims_version does not exist\n"
    "LINE 1: ...pode_lancar_avulso, motoboys_1.avulso_exige_foto, motoboys_1...\n"
    "[SQL: SELECT users.id AS users_id, users.email ... FROM users LEFT OUTER JOIN motoboys ...]\n"
    "[parameters: {'id_1': 382}]\n"
    "(Background on this error at: https://sqlalche.me/e/20/f405)"
)


def test_sql_dump_is_detected():
    assert looks_like_internal_error_dump(HOMOLOG_SQL_DUMP) is True


def test_unhandled_500_never_returns_sql():
    body = public_error_body(500, HOMOLOG_SQL_DUMP, unhandled=True)
    assert body == {"detail": CLIENT_SAFE_500}
    assert "psycopg" not in body["detail"].lower()
    assert "SELECT" not in body["detail"]


def test_http_500_tecnico_e_sanitizado():
    body = public_error_body(500, HOMOLOG_SQL_DUMP)
    assert body["detail"] == CLIENT_SAFE_500


def test_http_500_operacional_curto_permanece():
    body = public_error_body(500, "Falha ao gerar etiqueta.")
    assert body["detail"] == "Falha ao gerar etiqueta."


def test_http_400_de_negocio_permanece():
    body = public_error_body(400, "Pacote já coletado.")
    assert body["detail"] == "Pacote já coletado."


def test_http_400_estruturado_permanece():
    detail = {"mensagem": "Deseja ajudar?", "pode_ajudar": True}
    body = public_error_body(409, detail)
    assert body["detail"] == detail
