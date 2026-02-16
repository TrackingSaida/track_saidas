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
                "group": "leituras",
                "coleta_only": True,
                "coleta_manual_ok": False
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
            {
                "label": "Gerar Etiqueta",
                "href": "tracking-etiquetas.html",
                "roles": [0, 1, 2, 3],
                "group": "etiquetas"            
            },
        ]
    },
    {
        "section": "Financeiro",
        "icon": "ri-money-dollar-circle-line",
        "roles": [0, 1],
        "items": [
            {"label": "Fechamento Bases", "href": "tracking-coletas-resumo.html", "roles": [0, 1], "coleta_only": True, "coleta_manual_ok": True},
            {"label": "Fechamento de Motoboys", "href": "tracking-entregadores-resumo.html", "roles": [0, 1]},
            {"label": "Contabilidade", "href": "tracking-contabilidade.html", "roles": [0, 1]},
        ]
    },
    {
        "section": "Indicadores",
        "icon": "ri-dashboard-2-line",
        "roles": [0, 1, 2, 3],
        "items": [
            {"label": "Admin", "href": "dashboard-admin.html", "roles": [0]},
            {"label": "Visão 360", "href": "dashboard-visao-360.html", "roles": [0, 1], "visao360_only": True},
            {"label": "Coletas", "href": "dashboard-coletas.html", "roles": [0, 1], "coleta_only": True, "coleta_manual_ok": True},
            {"label": "Saídas", "href": "dashboard-saidas.html", "roles": [0, 1]},
            {"label": "Financeiro", "href": "dashboard-financeiro.html", "roles": [0, 1]},
        ]
    },

    {
        "section": "Cadastros",
        "icon": "ri-user-settings-line",
        "roles": [0, 1],
        "items": [
            {"label": "Entregadores", "href": "tracking-entregador.html", "roles": [0, 1]},
            {"label": "Bases",        "href": "tracking-base.html",       "roles": [0, 1], "coleta_only": True, "coleta_manual_ok": True},
            {"label": "Usuários",     "href": "tracking-usuarios.html",   "roles": [0, 1]},
            {"label": "Preços de Entrega", "href": "tracking-valores-entrega.html", "roles": [0, 1]},
        ]
    },
    {
        "section": "Configuração",
        "icon": "ri-settings-3-line",
        "roles": [0],
        "items": [
            {"label": "Owners", "href": "admin-owners.html", "roles": [0]},
        ]
    },
]


# =============================
# FUNÇÃO QUE MONTA MENU FINAL
# =============================

def menu_for_role(role: int, ignorar_coleta: bool = False, modo_operacao: str = "codigo"):
    visible_sections = []

    for section in MENU_DEFS:
        # Se o usuário pode ver a seção inteira
        if role in section["roles"]:
            # Filtra os itens permitidos (role + ignorar_coleta + modo_operacao)
            # coleta_only: ocultar quando ignorar_coleta, EXCETO se coleta_manual_ok e modo=coleta_manual
            # visao360_only: sempre ocultar quando ignorar_coleta
            allowed_items = []
            for item in section["items"]:
                if "roles" in item and role not in item["roles"]:
                    continue
                if ignorar_coleta and item.get("visao360_only"):
                    continue
                if ignorar_coleta and item.get("coleta_only"):
                    if item.get("coleta_manual_ok") and modo_operacao == "coleta_manual":
                        allowed_items.append(item)
                    continue
                allowed_items.append(item)

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
    ignorar_coleta = bool(getattr(user, "ignorar_coleta", False))
    modo_operacao = getattr(user, "modo_operacao", None) or "codigo"
    menu = menu_for_role(role, ignorar_coleta, modo_operacao)

    return {
        "role": role,
        "menu": menu
    }
