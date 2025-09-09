@router.get("/", response_model=List[EntregadorOut])
def list_entregadores(
    status: Optional[str] = Query("todos", description="Filtrar por status: ativo, inativo ou todos"),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """
    (1) identifica o usuário via cookie
    (2) resolve a base na tabela 'users'
    (3) busca todos os entregadores daquela base
    (4) aplica filtro de status se informado
    """
    base_user = _resolve_user_base(db, current_user)

    stmt = select(Entregador).where(Entregador.base == base_user)

    if status == "ativo":
        stmt = stmt.where(Entregador.ativo.is_(True))
    elif status == "inativo":
        stmt = stmt.where(Entregador.ativo.is_(False))
    # se "todos" ou inválido → não filtra

    stmt = stmt.order_by(Entregador.nome)
    rows = db.execute(stmt).scalars().all()
    return rows
