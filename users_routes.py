# CREATE (sempre novo registro; id gerado pelo banco)
@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(body: UserFields, db: Session = Depends(get_db)):
    obj = User(
        email=body.email,
        senha=body.senha,
        username=body.username,
        contato=body.contato,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)  # aqui o id já vem do banco (IDENTITY)
    return {"ok": True, "action": "created", "id": obj.id}
