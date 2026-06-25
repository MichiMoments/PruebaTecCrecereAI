"""Configuración central del agente de cobranza.

Todos los valores que es probable que cambien (modelo, rutas, límites,
referencia de proceso) viven aquí como constantes para que ajustarlos sea
trivial y no haya "números mágicos" dispersos por el código.
"""

from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# --- LLM -------------------------------------------------------------------
MODEL_NAME: str = "gemini-2.5-flash"
API_KEY_ENV: str = "GEMINI_API_KEY"
MAX_API_RETRIES: int = 3
API_BACKOFF_BASE_SECONDS: float = 1.5
MAX_TOOL_ITERATIONS: int = 8
SNAPSHOT_DIR: Path = Path(__file__).resolve().parent / "snapshots"
MAX_VALIDATION_ATTEMPTS: int = 3
PROCESS_REF: str = "Ticket-1234"
COMPANY_NAME: str = "Creceré"
