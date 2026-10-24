"""Ciclo de fechamento da base e resolução da chave PIX a receber.

Nenhum (nulo) não valida o intervalo. Semanal, quinzenal e mensal exigem
exatamente um ciclo civil fechado.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

PERIODOS_FECHAMENTO = ("semanal", "quinzenal", "mensal")
MOTIVO_PERIODO_FECHAMENTO = "PERIODO_FECHAMENTO"

_ROTULO_CICLO = {
    "semanal": "semana (segunda a domingo)",
    "quinzenal": "quinzena (dia 1 ao 15, ou dia 16 ao fim do mês)",
    "mensal": "mês (dia 1 ao último dia)",
}


def normalizar_periodo_fechamento(valor: Optional[str]) -> Optional[str]:
    """Nulo, vazio e 'nenhum' viram ausência de ciclo. Valor fora da lista é inválido."""
    if valor is None:
        return None
    texto = str(valor).strip().lower()
    if texto in ("", "nenhum"):
        return None
    if texto not in PERIODOS_FECHAMENTO:
        raise ValueError("periodo_fechamento deve ser semanal, quinzenal, mensal ou vazio.")
    return texto


TIPOS_PIX = ("cpf", "cnpj", "telefone", "email", "aleatoria")
ROTULO_TIPO_PIX = {
    "cpf": "CPF",
    "cnpj": "CNPJ",
    "telefone": "Celular",
    "email": "E-mail",
    "aleatoria": "Aleatória",
}


def normalizar_tipo_pix(valor: Optional[str]) -> Optional[str]:
    """Vazio significa sem tipo. Qualquer outro texto fora da lista é inválido."""
    if valor is None:
        return None
    texto = str(valor).strip().lower()
    if texto in ("", "nenhum"):
        return None
    if texto not in TIPOS_PIX:
        raise ValueError("Tipo da chave PIX deve ser CPF, CNPJ, celular, e-mail ou aleatória.")
    return texto


def _digitos(valor: str) -> str:
    return "".join(c for c in valor if c.isdigit())


def _cpf_valido(digitos: str) -> bool:
    if len(digitos) != 11 or digitos == digitos[0] * 11:
        return False

    def _dv(base: str, fator: int) -> int:
        total = sum(int(n) * f for n, f in zip(base, range(fator, 1, -1)))
        resto = (total * 10) % 11
        return 0 if resto == 10 else resto

    return _dv(digitos[:9], 10) == int(digitos[9]) and _dv(digitos[:10], 11) == int(digitos[10])


def _cnpj_valido(digitos: str) -> bool:
    if len(digitos) != 14 or digitos == digitos[0] * 14:
        return False

    def _dv(base: str, pesos: list) -> int:
        resto = sum(int(n) * p for n, p in zip(base, pesos)) % 11
        return 0 if resto < 2 else 11 - resto

    pesos_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos_2 = [6, *pesos_1]
    return _dv(digitos[:12], pesos_1) == int(digitos[12]) and _dv(digitos[:13], pesos_2) == int(digitos[13])


def _canonizar_chave(tipo: str, chave: str) -> str:
    if tipo == "cpf":
        digitos = _digitos(chave)
        if not _cpf_valido(digitos):
            raise ValueError("CPF inválido.")
        return digitos
    if tipo == "cnpj":
        digitos = _digitos(chave)
        if not _cnpj_valido(digitos):
            raise ValueError("CNPJ inválido.")
        return digitos
    if tipo == "telefone":
        digitos = _digitos(chave)
        if digitos.startswith("55") and len(digitos) == 13:
            digitos = digitos[2:]
        if len(digitos) != 11 or digitos[2] != "9" or digitos[0] == "0":
            raise ValueError("Celular inválido. Use DDD e os 9 dígitos.")
        return f"+55{digitos}"
    if tipo == "email":
        email = chave.strip().lower()
        if len(email) > 77 or " " in email or email.count("@") != 1:
            raise ValueError("E-mail inválido.")
        local, dominio = email.split("@", 1)
        if not local or "." not in dominio or dominio.startswith(".") or dominio.endswith("."):
            raise ValueError("E-mail inválido.")
        return email
    hexadec = "".join(c for c in chave.lower() if c in "0123456789abcdef")
    if len(hexadec) != 32:
        raise ValueError("Chave aleatória inválida.")
    return f"{hexadec[:8]}-{hexadec[8:12]}-{hexadec[12:16]}-{hexadec[16:20]}-{hexadec[20:]}"


def aplicar_chave_pix(
    tipo_informado: Optional[str],
    chave_informada: Optional[str],
    tipo_atual: Optional[str],
    chave_atual: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Devolve (tipo, chave canônica). Os dois vazios limpam. Chave antiga sem tipo permanece."""
    tipo = normalizar_tipo_pix(tipo_informado)
    chave = (chave_informada or "").strip()
    if tipo is None and not chave:
        return None, None
    if tipo is None:
        atual = (chave_atual or "").strip()
        if atual and chave == atual and normalizar_tipo_pix(tipo_atual) is None:
            return None, atual
        raise ValueError("Informe o tipo da chave PIX.")
    if not chave:
        raise ValueError("Informe a chave PIX.")
    return tipo, _canonizar_chave(tipo, chave)


def resolver_pix_receber(
    chave_base: Optional[str],
    tipo_base: Optional[str],
    chave_global: Optional[str],
    tipo_global: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """A chave da base prevalece junto com o tipo dela. Sem as duas chaves, não há PIX."""
    base = (chave_base or "").strip()
    if base:
        try:
            tipo = normalizar_tipo_pix(tipo_base)
        except ValueError:
            tipo = None
        return base, tipo
    global_pix = (chave_global or "").strip()
    if not global_pix:
        return None, None
    try:
        tipo = normalizar_tipo_pix(tipo_global)
    except ValueError:
        tipo = None
    return global_pix, tipo


def texto_pix_relatorio(tipo: Optional[str], chave: Optional[str]) -> Optional[str]:
    """Linha do PDF. Com tipo: PIX CPF {chave}. Sem tipo, só a chave já gravada."""
    valor = (chave or "").strip()
    if not valor:
        return None
    try:
        codigo = normalizar_tipo_pix(tipo)
    except ValueError:
        codigo = None
    rotulo = ROTULO_TIPO_PIX.get(codigo or "", "")
    if rotulo:
        return f"PIX {rotulo} {valor}"
    return f"PIX {valor}"


def resolver_chave_pix(chave_base: Optional[str], chave_global: Optional[str]) -> Optional[str]:
    """A chave da base prevalece. Sem as duas, não há PIX no relatório."""
    chave, _tipo = resolver_pix_receber(chave_base, None, chave_global, None)
    return chave


def ciclo_corresponde(periodo: Optional[str], inicio: date, fim: date) -> bool:
    """Nenhum corresponde a qualquer intervalo. Os demais exigem um ciclo exato."""
    codigo = normalizar_periodo_fechamento(periodo)
    if codigo is None:
        return True
    if fim < inicio:
        return False
    if codigo == "semanal":
        return inicio.weekday() == 0 and fim == inicio + timedelta(days=6)
    if inicio.year != fim.year or inicio.month != fim.month:
        return False
    ultimo = calendar.monthrange(inicio.year, inicio.month)[1]
    if codigo == "quinzenal":
        return (inicio.day == 1 and fim.day == 15) or (inicio.day == 16 and fim.day == ultimo)
    return inicio.day == 1 and fim.day == ultimo


def aviso_ciclo_fechamento(
    nome_base: str,
    periodo: Optional[str],
    inicio: date,
    fim: date,
) -> Optional[str]:
    """Texto para o operador quando o intervalo não é o ciclo da base. Nulo se pode gerar."""
    if ciclo_corresponde(periodo, inicio, fim):
        return None
    codigo = normalizar_periodo_fechamento(periodo)
    rotulo = _ROTULO_CICLO.get(codigo or "", "período configurado")
    nome = (nome_base or "Esta base").strip() or "Esta base"
    return (
        f"{nome} fecha por {rotulo}. "
        f"O período {inicio:%d/%m/%Y} a {fim:%d/%m/%Y} não corresponde. "
        "Ajuste o período para gerar o fechamento."
    )


def renome_fechamento_bloqueado(periodos_antigos: list, periodos_novos: list, mesma_chave: bool) -> bool:
    """Dois fechamentos no mesmo período não podem ficar com o mesmo nome."""
    vistos = set()
    for periodo in periodos_antigos:
        if periodo in vistos:
            return True
        vistos.add(periodo)
    if mesma_chave:
        return False
    return bool(vistos & set(periodos_novos))
