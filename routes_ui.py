from fastapi import APIRouter, Depends
from deps import current_user_with_role

MENU_DEFS = [
    {
        "section": "Operação",
        "icon": "las la-motorcycle",
        "roles": [0, 1, 2, 3],   # root (0) tem acesso total
        "items": [
            {"label": "Leitura de Coletas",  "href": "tracking-coleta-leitura.html", "roles": [0, 1, 2, 3]},
            {"label": "Resumo de Coletas",   "href": "tracking-coletas-resumo.html", "roles": [0, 1, 2]},
            {"label": "Leitura de Saídas",   "href": "tracking-leitura.html",        "roles": [0, 1, 2, 3]},
            {"label": "Registros de Saídas", "href": "tracking-registros.html",      "roles": [0, 1, 2, 3]},
        ]
    },

    {
        "section": "Dashboards",
        "icon": "ri-dashboard-2-line",
        "roles": [0, 1, 2, 3],  # root enxerga tudo
        "items": [
            {"label": "Visão geral", "href": "dashboard-tracking-overview.html", "roles": [0, 1]},
            {"label": "Ranking",     "href": "dashboard-tracking-saidas.html",   "roles": [0, 1, 2, 3]},
        ]
    },

    {
        "section": "Configurações",
        "icon": "ri-settings-3-line",
        "roles": [0, 1, 2],  # root também vê a seção inteira
        "items": [
            {"label": "Entregadores", "href": "tracking-entregador.html", "roles": [0, 1, 2]},
            {"label": "Bases",        "href": "tracking-base.html",       "roles": [0, 1, 2]},
            {"label": "Usuários",     "href": "tracking-usuarios.html",   "roles": [0, 1]},   # root enxerga
            {"label": "Owners",       "href": "admin-owners.html",        "roles": [0]},      # exclusivo do root
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

