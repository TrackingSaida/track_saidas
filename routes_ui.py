# routes_ui.py
from fastapi import APIRouter, Depends
from deps import get_current_user
from ui_menu import menu_for_role

router = APIRouter(prefix="/ui", tags=["UI"])

@router.get("/menu")
def get_menu(user = Depends(get_current_user)):
    role = int(user.role)
    return {"role": role, "menu": menu_for_role(role)}
