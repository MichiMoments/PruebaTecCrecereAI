"""Backend falso en memoria (la "base de datos").

Simula el sistema de cartera de la entidad. Contiene los deudores semilla y
utilidades de normalización/lookup. No conoce nada del LLM ni del estado de la
conversación: solo responde consultas de datos crudos.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Optional, TypedDict


class DebtorRecord(TypedDict):
    """Registro crudo de un deudor tal como lo guardaría el core de cartera.

    El orden refleja dos bloques: primero la **identificación** del deudor y
    luego la **obligación** (la deuda).
    """

    # --- Identificación del deudor ---
    tipo_documento: str     # "CC", "CE", ... (tipo de documento)
    documento: str          # número normalizado (solo dígitos)
    nombre: str             # nombre legal completo
    fecha_nacimiento: str   # YYYY-MM-DD (segundo factor de validación)
    # --- Obligación / deuda ---
    producto: str           # producto asociado a la deuda
    saldo: int              # saldo en COP (entero)
    dias_mora: int          # días de mora
    fecha_corte: str        # fecha de corte YYYY-MM-DD


def normalizar_documento(documento: str) -> str:
    """Normaliza una cédula dejando solo dígitos.

    Así ``1.082.260.472`` y ``1082260472`` (o con espacios) resuelven al
    mismo registro.
    """
    return re.sub(r"\D", "", documento or "")


def normalizar_nombre(nombre: str) -> str:
    """Normaliza un nombre para comparación case/acento-insensible.

    Pasa a minúsculas, elimina tildes/diacríticos y colapsa espacios.
    """
    if not nombre:
        return ""
    sin_tildes = "".join(
        c
        for c in unicodedata.normalize("NFD", nombre)
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin_tildes.strip().lower())


def primer_nombre(nombre: str) -> str:
    """Devuelve solo el primer nombre (primer token) de un nombre completo.

    Se usa para saludar al deudor en la apertura de la llamada sin revelar su
    nombre completo. ``""`` si el nombre viene vacío.
    """
    partes = (nombre or "").split()
    return partes[0] if partes else ""


# --- Datos semilla ---------------------------------------------------------
# Varios deudores de prueba. La clave es la cédula ya normalizada (solo dígitos)
# y el orden de los campos sigue el del esquema: identificación primero, deuda
# después. Se cubren productos distintos y rangos de saldo/mora diferentes para
# ejercitar la gestión (validación, consulta de deuda, planes de pago, etc.).
# `1082260472` es el deudor de la sesión por defecto (ver `SESSION_DOCUMENTO` en
# main.py); para probar otro, cambia esa constante por cualquier cédula de aquí.
_DEUDORES: dict[str, DebtorRecord] = {
    # Tarjeta de crédito · mora alta · saldo medio.
    "1082260472": {
        "tipo_documento": "CC",
        "documento": "1082260472",
        "nombre": "Liliana Ospina Cano",
        "fecha_nacimiento": "1990-05-14",
        "producto": "Tarjeta de crédito",
        "saldo": 5_125_922,
        "dias_mora": 112,
        "fecha_corte": "2026-02-28",
    },
    # Crédito de libre inversión · mora temprana · saldo alto.
    "71345890": {
        "tipo_documento": "CC",
        "documento": "71345890",
        "nombre": "Carlos Andrés Restrepo Gómez",
        "fecha_nacimiento": "1985-11-02",
        "producto": "Crédito de libre inversión",
        "saldo": 9_850_000,
        "dias_mora": 45,
        "fecha_corte": "2026-05-31",
    },
    # Crédito de vehículo · mora media · saldo grande.
    "1020456789": {
        "tipo_documento": "CC",
        "documento": "1020456789",
        "nombre": "María Fernanda Quintero Salazar",
        "fecha_nacimiento": "1992-08-21",
        "producto": "Crédito de vehículo",
        "saldo": 18_200_000,
        "dias_mora": 78,
        "fecha_corte": "2026-04-30",
    },
    # Crédito educativo · mora baja · saldo pequeño · documento CE.
    "1144082356": {
        "tipo_documento": "CE",
        "documento": "1144082356",
        "nombre": "Jorge Iván Mejía Loaiza",
        "fecha_nacimiento": "1998-03-12",
        "producto": "Crédito educativo",
        "saldo": 3_480_000,
        "dias_mora": 23,
        "fecha_corte": "2026-05-31",
    },
}


def buscar_deudor(documento: str) -> Optional[DebtorRecord]:
    """Busca un deudor por cédula (normalizada). ``None`` si no existe."""
    return _DEUDORES.get(normalizar_documento(documento))


def listar_deudores() -> list[DebtorRecord]:
    """Devuelve todos los deudores sembrados (para listar los disponibles)."""
    return list(_DEUDORES.values())


def nombre_coincide(nombre_declarado: str, record: DebtorRecord) -> bool:
    """Compara el nombre declarado contra el del registro, normalizando ambos."""
    return normalizar_nombre(nombre_declarado) == normalizar_nombre(record["nombre"])


def fecha_nacimiento_coincide(declarada: str, record: DebtorRecord) -> bool:
    """Compara la fecha de nacimiento declarada (YYYY-MM-DD) con el registro.

    Es un segundo factor de validación. Si la fecha declarada no se puede
    interpretar como ``YYYY-MM-DD``, se considera que NO coincide.
    """
    try:
        d1 = datetime.strptime((declarada or "").strip(), "%Y-%m-%d").date()
        d2 = datetime.strptime(record["fecha_nacimiento"], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return d1 == d2
