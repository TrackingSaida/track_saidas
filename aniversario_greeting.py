"""Felicitação de aniversário no primeiro acesso do dia (payload para /auth/me)."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

TZ_SP = ZoneInfo("America/Sao_Paulo")

ASSINATURA = (
    "────────────────────\n"
    "Com carinho,\n"
    "Equipe Rotevo 💚"
)


@dataclass(frozen=True)
class AniversarioGreeting:
    titulo: str
    mensagem: str
    botao: str


def hoje_sao_paulo(now: Optional[datetime] = None) -> date:
    """Data civil em America/Sao_Paulo."""
    if now is None:
        return datetime.now(TZ_SP).date()
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_SP).astimezone(TZ_SP).date()
    return now.astimezone(TZ_SP).date()


def is_aniversario_hoje(
    data_nascimento: Optional[date],
    hoje: Optional[date] = None,
) -> bool:
    """
    True se mês/dia de nascimento coincidem com hoje (TZ SP).
    29/02 em ano não bissexto: celebra em 28/02.
    """
    if data_nascimento is None:
        return False
    ref = hoje if hoje is not None else hoje_sao_paulo()
    nasc_mes = data_nascimento.month
    nasc_dia = data_nascimento.day

    if nasc_mes == 2 and nasc_dia == 29:
        last_feb = monthrange(ref.year, 2)[1]
        if last_feb == 28:
            return ref.month == 2 and ref.day == 28
        return ref.month == 2 and ref.day == 29

    return ref.month == nasc_mes and ref.day == nasc_dia


def _primeiro_nome(nome: Optional[str]) -> str:
    parts = (nome or "").strip().split()
    return parts[0] if parts else ""


def _titulo_com_nome(nome: Optional[str], emoji: str) -> str:
    primeiro = _primeiro_nome(nome)
    if primeiro:
        return f"Feliz aniversário, {primeiro}! {emoji}"
    return f"Feliz aniversário! {emoji}"


def _mensagem_completa(corpo: str) -> str:
    return f"{corpo}\n\n{ASSINATURA}"


def build_aniversario_greeting(
    role: Optional[int],
    nome: Optional[str] = None,
) -> AniversarioGreeting:
    """Monta título, mensagem e botão conforme o perfil."""
    try:
        role_int = int(role) if role is not None else 2
    except (TypeError, ValueError):
        role_int = 2

    if role_int in (0, 1):
        return AniversarioGreeting(
            titulo=_titulo_com_nome(nome, "🎉"),
            mensagem=_mensagem_completa(
                "Hoje é dia de celebrar você, que faz a operação acontecer. "
                "Que seu novo ciclo seja repleto de conquistas, bons momentos e excelentes caminhos!"
            ),
            botao="Acessar meu painel",
        )

    if role_int == 4:
        return AniversarioGreeting(
            titulo=_titulo_com_nome(nome, "🛵"),
            mensagem=_mensagem_completa(
                "Hoje sua primeira rota é especial: celebrar você. "
                "Que seu novo ciclo tenha bons caminhos, muitas conquistas e que você esteja sempre em segurança!"
            ),
            botao="Bora pra rota!",
        )

    # operador (2), coletador (3) e demais
    return AniversarioGreeting(
        titulo=_titulo_com_nome(nome, "🥳"),
        mensagem=_mensagem_completa(
            "Antes de começar a operação, queremos celebrar este dia especial com você. "
            "Que seu novo ciclo seja leve, feliz e cheio de conquistas!"
        ),
        botao="Começar meu dia",
    )


def resolve_aniversario_payload(
    data_nascimento: Optional[date],
    role: Optional[int],
    nome: Optional[str] = None,
    hoje: Optional[date] = None,
) -> Optional[AniversarioGreeting]:
    """Retorna o payload de felicitação ou None se não for o dia."""
    if not is_aniversario_hoje(data_nascimento, hoje=hoje):
        return None
    return build_aniversario_greeting(role, nome=nome)
