# routes_ui.py
from fastapi import APIRouter, Depends
from deps import current_user_with_role
# defina seu menu aqui
MENU_DEFS = [
    {"section":"Operação","icon":"las la-motorcycle","roles":[1,2,3],"items":[
        {"label":"Registros","href":"tracking-registros.html"},
        {"label":"Leituras","href":"tracking-leitura.html"},
    ]},
    {"section":"Dashboards","icon":"ri-dashboard-2-line","roles":[1],"items":[
        {"label":"Tracking","href":"dashboard-tracking-saidas.html"},
    ]},
    {"section":"Configurações","icon":"ri-settings-3-line","roles":[1,2],"items":[
        {"label":"Entregadores","href":"tracking-entregador.html"},
    ]},
]

def menu_for_role(role:int):
    return [
        {"section": s["section"], "icon": s["icon"], "items": s["items"]}
        for s in MENU_DEFS if role in s["roles"]
    ]

router = APIRouter(prefix="/ui", tags=["UI"])

@router.get("/menu")
def get_menu(user = Depends(current_user_with_role)):
    return {"role": int(user.role), "menu": menu_for_role(int(user.role))}
