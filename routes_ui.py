from fastapi import APIRouter, Depends
from deps import current_user_with_role

MENU_DEFS = [
    {
        "section": "Operação",
        "icon": "las la-motorcycle",
        "roles": [1, 2, 3],
        "items": [
            {"label": "Leitura de Coletas",  "href": "tracking-coleta-leitura.html", "roles": [1, 2, 3]},
            {"label": "Resumo de Coletas",   "href": "tracking-coletas-resumo.html", "roles": [1, 2]}, 
            {"label": "Leitura de Saídas",   "href": "tracking-leitura.html", "roles": [1, 2, 3]},
            {"label": "Registros de Saídas", "href": "tracking-registros.html", "roles": [1, 2, 3]},      
        ]
    },
    {
        "section": "Dashboards",
        "icon": "ri-dashboard-2-line",
        "roles": [1],
        "items": [
            {"label": "Visão geral", "href": "dashboard-tracking-overview.html"},
        ]
    },
    {
        "section": "Configurações",
        "icon": "ri-settings-3-line",
        "roles": [1, 2],
        "items": [
            {"label": "Entregadores", "href": "tracking-entregador.html"},
            {"label": "Bases",        "href": "tracking-base.html"},
        ]
    },
]

def menu_for_role(role: int):
    visible_sections = []
    for s in MENU_DEFS:
        if role in s["roles"]:
            filtered_items = [i for i in s["items"] if "roles" not in i or role in i["roles"]]
            if filtered_items:
                visible_sections.append({
                    "section": s["section"],
                    "icon": s["icon"],
                    "items": filtered_items
                })
    return visible_sections

router = APIRouter(prefix="/ui", tags=["UI"])

from fastapi import Request

@router.get("/menu")
def get_menu(request: Request, user=Depends(current_user_with_role)):
    return {"role": int(user.role), "menu": menu_for_role(int(user.role))}

