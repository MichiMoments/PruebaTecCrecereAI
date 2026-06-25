# Agente de Cobranza Conversacional (consola, Python + Gemini)

Agente de línea de comandos que ejecuta una llamada de cobranza contra un
deudor ficticio, íntegramente en español de Colombia En una sesión el
agente valida identidad, explica la deuda, propone planes, negocia objeciones,
registra el compromiso de pago y cierra la gestión. El LLM (Google Gemini
es quien decide qué herramienta llamar y con qué argumentos; el código solo
despacha lo que el modelo pide.

---

## Cómo ejecutar

Requisitos: Python 3.10 y una API key de Google AI Studio.

```bash
# 1) Entorno virtual
python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

# 2) Dependencias
pip install -r requirements.txt

# 3) API key — REQUERIDA para ejecutar (variable GEMINI_API_KEY)
# Opción A (recomendada) — crea tu .env a partir de la plantilla y reemplaza
# el valor de ejemplo por tu key real de Google AI Studio:
cp .env.example .env          # Windows (PowerShell): copy .env.example .env
# luego edita .env y cambia "tu_api_key_aqui" por tu key:
#   GEMINI_API_KEY=AIza...tu_key_real...
#
# Opción B — exportarla en la shell en vez de usar .env:
# Windows (PowerShell):
$env:GEMINI_API_KEY = "tu_api_key"
# Linux/macOS:
export GEMINI_API_KEY="tu_api_key"

# 4) Ejecutar (deudor por defecto)
python main.py

# ...o gestionar otro deudor sembrado pasando su cédula (con o sin puntos):
python main.py 71345890
```
 

Para terminar la sesión escribe `salir` / `exit` (o `Ctrl-C`). Al cerrar se
imprime un resumen y la línea de tiempo del historial El perfil se persiste
en `snapshots/perfil_1082260472.json`.

El modelo por defecto es `gemini-2.5-flash` y vive como única constante en
[`config.py`](config.py)(`MODEL_NAME`).

