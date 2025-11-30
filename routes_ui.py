# =============================
# routes_ui.py  (VERSÃO FINAL)
# =============================

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from deps import current_user_with_role
from models import User


# =============================
# DEFINIÇÃO DO MENU
# =============================

MENU_DEFS = [
    {
        "section": "Operação",
        "icon": "las la-motorcycle",
        "roles": [0, 1, 2, 3],   # root (0) vê tudo
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
        "roles": [0, 1, 2, 3],  # root também vê
        "items": [
            {"label": "Visão geral", "href": "dashboard-tracking-overview.html", "roles": [0, 1]},
            {"label": "Ranking",     "href": "dashboard-tracking-saidas.html",   "roles": [0, 1, 2, 3]},
        ]
    },

    {
        "section": "Configurações",
        "icon": "ri-settings-3-line",
        "roles": [0, 1, 2],  # root pode tudo
        "items": [
            {"label": "Entregadores", "href": "tracking-entregador.html", "roles": [0, 1, 2]},
            {"label": "Bases",        "href": "tracking-base.html",       "roles": [0, 1, 2]},
            {"label": "Usuários",     "href": "tracking-usuarios.html",   "roles": [0, 1]},
            {"label": "Owners",       "href": "admin-owners.html",        "roles": [0]},  # exclusivo root
        ]
    },
]


# =============================
# FUNÇÃO QUE MONTA MENU FINAL
# =============================

def menu_for_role(role: int):
    visible_sections = []

    for s in MENU_DEFS:
        # Se a role pode ver a seção
        if role in s["roles"]:
            # filtra apenas os itens permitidos
            allowed_items = []
            for i in s["items"]:
                if "roles" not in i or role in i["roles"]:
                    allowed_items.append(i)

            # só inclui se houver itens
            if allowed_items:
                visible_sections.append({
                    "section": s["section"],
                    "icon": s["icon"],
                    "items": allowed_items
                })

    return visible_sections


# =============================
# ROUTER
# =============================

router = APIRouter(prefix="/ui", tags=["UI"])

@router.get("/menu")
def get_menu(user=Depends(current_user_with_role)):
    """Retorna o menu completo baseado no nível do usuário"""
    role = int(user.role)
    menu = menu_for_role(role)
    return {"role": role, "menu": menu}
