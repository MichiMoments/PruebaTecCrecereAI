"""Backend falso en memoria (la "base de datos").

Simula el sistema de cartera de la entidad. Contiene los deudores semilla y
utilidades de normalización/lookup. No conoce nada del LLM ni del estado de la
conversación: solo responde consultas de datos crudos.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, TypedDict


class DebtorRecord(TypedDict):
    """Registro crudo de un deudor tal como lo guardaría el core de cartera."""

    documento: str          # cédula normalizada (solo dígitos)
    nombre: str             # nombre legal completo
    saldo: int              # saldo en COP (entero)
    dias_mora: int          # días de mora
    producto: str           # producto asociado a la deuda
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


# --- Datos semilla ---------------------------------------------------------
# Un único deudor sembrado. La cédula se guarda ya normalizada.
_DEUDORES: dict[str, DebtorRecord] = {
    "1082260472": {
        "documento": "1082260472",
        "nombre": "Liliana Ospina Cano",
        "saldo": 5_125_922,
        "dias_mora": 112,
        "producto": "Tarjeta de crédito",
        "fecha_corte": "2026-02-28",
    },
}


def buscar_deudor(documento: str) -> Optional[DebtorRecord]:
    """Busca un deudor por cédula (normalizada). ``None`` si no existe."""
    return _DEUDORES.get(normalizar_documento(documento))


def nombre_coincide(nombre_declarado: str, record: DebtorRecord) -> bool:
    """Compara el nombre declarado contra el del registro, normalizando ambos."""
    return normalizar_nombre(nombre_declarado) == normalizar_nombre(record["nombre"])
