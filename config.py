"""Configuración central del agente de cobranza.

Todos los valores que es probable que cambien (modelo, rutas, límites,
referencia de proceso) viven aquí como constantes para que ajustarlos sea
trivial y no haya "números mágicos" dispersos por el código.
"""

from __future__ import annotations

from pathlib import Path

# Carga un archivo .env (si existe) junto a este módulo, para que GEMINI_API_KEY
# pueda vivir ahí sin tener que exportarla a mano. python-dotenv es opcional: si
# no está instalado, simplemente se usan las variables de entorno del sistema.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# --- LLM -------------------------------------------------------------------
# Nombre del modelo Gemini *flash* a usar. Es un único punto de cambio:
# si el nombre del modelo cambia en el futuro, solo se edita aquí.
# (Alternativa más económica: "gemini-2.5-flash-lite". NOTA: "gemini-2.0-flash"
# fue dado de baja el 2026-06-01 y ya no debe usarse.)
MODEL_NAME: str = "gemini-2.5-flash"

# Variable de entorno desde la que se lee la API key de Google AI Studio.
API_KEY_ENV: str = "GEMINI_API_KEY"

# Reintentos ante errores de red / rate-limit del API, con backoff simple.
MAX_API_RETRIES: int = 3
API_BACKOFF_BASE_SECONDS: float = 1.5

# Tope de iteraciones del bucle de tool-calling por turno del usuario, para
# evitar bucles infinitos si el modelo se queda pidiendo herramientas.
MAX_TOOL_ITERATIONS: int = 8

# --- Persistencia ----------------------------------------------------------
# Carpeta donde se reescribe el perfil del deudor en cada mutación.
SNAPSHOT_DIR: Path = Path(__file__).resolve().parent / "snapshots"

# --- Reglas de negocio -----------------------------------------------------
# Intentos máximos de validación de identidad antes de cerrar la gestión.
MAX_VALIDATION_ATTEMPTS: int = 3

# Referencia interna del proceso (ticket CRC-5922). Debe quedar estampada en
# todo compromiso de pago registrado.
PROCESS_REF: str = "CRC-5922"

# Identidad del agente: nombre de la empresa con la que se presenta. El agente
# se identifica como el asistente virtual (IA) de esta entidad.
COMPANY_NAME: str = "Creceré"
