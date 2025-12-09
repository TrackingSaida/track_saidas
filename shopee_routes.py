from datetime import datetime, timedelta
from models import ShopeeToken

# ...

@router.get(
    "/callback",
    summary="Callback da Shopee (troca code por tokens)",
)
def shopee_callback(
    code: str = Query(...),
    shop_id: int = Query(...),
    main_account_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    host, partner_id, partner_key, _ = _get_shopee_config()

    path = "/api/v2/auth/token/get"
    timestamp = int(time.time())
    sign = _generate_sign(partner_id, partner_key, path, timestamp)

    url = (
        f"{host}{path}"
        f"?partner_id={partner_id}"
        f"&timestamp={timestamp}"
        f"&sign={sign}"
    )

    payload = {
        "code": code,
        "shop_id": shop_id,
        "partner_id": partner_id,
    }

    resp = requests.post(url, json=payload, timeout=20)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail={"msg": "Erro ao obter token Shopee", "body": resp.text},
        )

    data = resp.json()

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expire_in") or data.get("expires_in")

    if not access_token:
        raise HTTPException(
            status_code=400,
            detail={"msg": "Shopee não retornou access_token", "body": data},
        )

    expires_at = None
    if expires_in:
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    # Já existe token para essa shop?
    existente = (
        db.query(ShopeeToken)
        .filter(ShopeeToken.shop_id == shop_id)
        .first()
    )

    if existente:
        existente.access_token = access_token
        existente.refresh_token = refresh_token or existente.refresh_token
        existente.expires_at = expires_at
        existente.main_account_id = main_account_id
        db.commit()
        db.refresh(existente)
    else:
        novo = ShopeeToken(
            shop_id=shop_id,
            main_account_id=main_account_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
        db.add(novo)
        db.commit()
        db.refresh(novo)

    return {
        "msg": "Tokens da Shopee salvos/atualizados com sucesso.",
        "shop_id": shop_id,
        "main_account_id": main_account_id,
        "expires_at": expires_at,
        "raw": data,
    }
