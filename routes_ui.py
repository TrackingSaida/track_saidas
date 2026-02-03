# =============================
# routes_ui.py  (VERSÃO AJUSTADA E FINAL)
# =============================

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import get_current_user
from models import User


# =============================
# DEFINIÇÃO DO MENU
# =============================

MENU_DEFS = [
    {
        "section": "Operação",
        "icon": "las la-motorcycle",
        "roles": [0, 1, 2, 3],
        "items": [
            {
                "label": "Registrar Coletas",
                "href": "tracking-coleta-leitura.html",
                "roles": [0, 1, 2, 3],
                "group": "leituras"
            },
            {
                "label": "Registrar Saídas",
                "href": "tracking-leitura.html",
                "roles": [0, 1, 2, 3],
                "group": "leituras"
            },
            {
                "label": "Registros Gerais",
                "href": "tracking-registros.html",
                "roles": [0, 1, 2, 3],
                "group": "registros"
            },
        ]
    },
    {
        "section": "Financeiro",
        "icon": "ri-money-dollar-circle-line",
        "roles": [0, 1],
        "items": [
            {
                "label": "Resumo de Coletas",
                "href": "tracking-coletas-resumo.html",
                "roles": [0, 1],
                "group": "resumos"
            },
            {
                "label": "Resumo por Entregador",
                "href": "tracking-entregadores-resumo.html",
                "roles": [0, 1],
                "group": "resumos"
            },
        ]
    },
    {
        "section": "Dashboards",
        "icon": "ri-dashboard-2-line",
        "roles": [0, 1, 2, 3],
        "items": [
            {"label": "Visão geral", "href": "dashboard-tracking-overview.html", "roles": [0, 1]},
            {"label": "Ranking",     "href": "dashboard-tracking-saidas.html",   "roles": [0, 1, 2, 3]},
        ]
    },

    {
        "section": "Configurações",
        "icon": "ri-settings-3-line",
        "roles": [0, 1, 2],  # root tem tudo
        "items": [
            {"label": "Entregadores", "href": "tracking-entregador.html", "roles": [0, 1, 2]},
            {"label": "Bases",        "href": "tracking-base.html",       "roles": [0, 1]},
            {"label": "Usuários",     "href": "tracking-usuarios.html",   "roles": [0, 1]},
            {"label": "Valores de Entrega", "href": "tracking-valores-entrega.html", "roles": [0, 1]},
            {"label": "Owners",       "href": "admin-owners.html",        "roles": [0]},  # exclusivo root
        ]
    },
]


# =============================
# FUNÇÃO QUE MONTA MENU FINAL
# =============================

def menu_for_role(role: int):
    visible_sections = []

    for section in MENU_DEFS:
        # Se o usuário pode ver a seção inteira
        if role in section["roles"]:
            # Filtra os itens permitidos
            allowed_items = [
                item for item in section["items"]
                if ("roles" not in item or role in item["roles"])
            ]

            if allowed_items:
                visible_sections.append({
                    "section": section["section"],
                    "icon": section["icon"],
                    "items": allowed_items
                })

    return visible_sections


# =============================
# ROUTER
# =============================

router = APIRouter(prefix="/ui", tags=["UI"])


@router.get("/menu")
def get_menu(user: User = Depends(get_current_user)):
    """
    Retorna o menu baseado no nível do usuário (role).
    Usa get_current_user diretamente, garantindo compatibilidade com /auth/me.
    """
    role = int(user.role)
    menu = menu_for_role(role)

    return {
        "role": role,
        "menu": menu
    }
