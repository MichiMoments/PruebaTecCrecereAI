# Agente de Cobranza Conversacional (consola, Python + Gemini)

Agente de línea de comandos que ejecuta una llamada de **cobranza** contra un
deudor ficticio, íntegramente en **español de Colombia**. En una sesión el
agente valida identidad, explica la deuda, propone planes, negocia objeciones,
registra el compromiso de pago y cierra la gestión. El **LLM (Google Gemini)**
es quien decide qué herramienta llamar y con qué argumentos; el código solo
despacha lo que el modelo pide.

---

## Cómo ejecutar

Requisitos: **Python 3.10+** y una **API key de Google AI Studio** (no Vertex/GCP).

```bash
# 1) Entorno virtual
python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

# 2) Dependencias
pip install -r requirements.txt

# 3) API key (variable de entorno GEMINI_API_KEY)
# Opción A — archivo .env en deuda_agent/ (se carga automáticamente):
#   GEMINI_API_KEY=tu_api_key
# Opción B — exportarla en la shell:
# Windows (PowerShell):
$env:GEMINI_API_KEY = "tu_api_key"
# Linux/macOS:
export GEMINI_API_KEY="tu_api_key"

# 4) Ejecutar
python main.py
```

> El programa carga automáticamente un archivo `.env` ubicado junto al código
> (vía `python-dotenv`) y, si no existe, usa las variables de entorno del
> sistema. No hay ninguna key embebida en el código. **No subas tu `.env` a un
> repositorio.**

Para terminar la sesión escribe `salir` / `exit` (o `Ctrl-C`). Al cerrar se
imprime un resumen y la **línea de tiempo del historial**. El perfil se persiste
en `snapshots/perfil_1082260472.json`.

El modelo por defecto es `gemini-2.5-flash` y vive como única constante en
[`config.py`](config.py) (`MODEL_NAME`); si quieres abaratar puedes cambiarlo a
`gemini-2.5-flash-lite`. (`gemini-2.0-flash` fue dado de baja el 2026-06-01.)

---

## Supuestos (implementados y asumidos)

- **Deudor semilla:** Liliana Ospina Cano · cédula `1.082.260.472` · saldo
  `5 125 922` COP · `112` días de mora · producto **Tarjeta de crédito** ·
  fecha de corte `2026-02-28` (ver [`data.py`](data.py)).
- **Normalización de cédula:** se ignoran puntos/espacios, así `1082260472` y
  `1.082.260.472` resuelven al mismo registro.
- **Validez de identidad:** es válida solo si **el nombre declarado coincide Y
  la cédula coincide** con el registro. La comparación de nombre es
  *insensible a mayúsculas y tildes*. Tras `MAX_VALIDATION_ATTEMPTS = 3` intentos
  fallidos, la gestión pasa a `IDENTIDAD_NO_VALIDADA` y se cierra.
- **Cédula desconocida / mal formada:** devuelve un error estructurado
  `NO_ENCONTRADO`, de modo que esa rama es ejercitable.
- **Dinero** entero en COP; **fechas** en formato `YYYY-MM-DD` (el compromiso
  debe ser una fecha futura y un monto ≤ saldo).
- **Una corrida = una sesión de un deudor**, persistida en su propio JSON.
- La sesión gestiona la obligación de la cédula `1082260472`; el agente abre la
  llamada y solicita la validación de identidad de la persona.

---

## Decisiones de arquitectura (y por qué)

### Separación en tres capas
- **`agent.py` (modelo + orquestación):** habla con Gemini, mantiene el
  historial de la conversación y ejecuta el **bucle manual de tool-calling**.
  *No lee ni muta el perfil*; solo despacha herramientas.
- **`tools.py` (herramientas):** la única superficie que el modelo puede
  ejecutar. Cada herramienta valida sus argumentos y devuelve un objeto
  JSON-serializable (incluido un error estructurado). Mutan el estado **solo** a
  través de `StateManager`.
- **`state.py` (estado):** dataclasses + enums del perfil y un `StateManager`
  que es el **único escritor**: audita cada cambio en `historial` y reescribe el
  JSON en disco.

Capas de soporte: [`data.py`](data.py) (backend falso en memoria) y
[`config.py`](config.py) (constantes).

### El modelo —no un `if`— elige las herramientas
El bucle de [`agent.py`](agent.py) declara los esquemas, manda el mensaje y, si
`response.function_calls` trae llamadas, **ejecuta exactamente las que el modelo
pidió** (`CobranzaTools.dispatch`) y le devuelve los resultados con
`Part.from_function_response`; repite hasta que el modelo responde en texto. Se
usa **función-calling manual** (con
`AutomaticFunctionCallingConfig(disable=True)`) en lugar de la AFC automática del
SDK, precisamente para que sea **explícito y demostrable** que es el modelo quien
dirige la selección de herramientas. No existe ninguna capa de reglas externa
que decida qué herramienta llamar.

### Mecanismo híbrido de actualización "al vuelo"
El perfil debe actualizarse *durante* la conversación, no al final. Para lograrlo
sin una capa de reglas externa, **todo cambio de campo fluye por una herramienta**:

- Las **herramientas de negocio** requeridas tienen efectos colaterales sobre el
  perfil: `consultar_deuda` cachea la deuda, `registrar_compromiso_pago` graba el
  compromiso, `actualizar_estado_gestion` fija el estado.
- Se añaden **herramientas de perfil** pequeñas —`validar_identidad`,
  `registrar_objecion`, `registrar_disposicion`— para que también los campos *no
  comerciales* (identidad, objeciones, disposición) se actualicen vía una llamada
  del modelo y no por extracción posterior.

Cada herramienta escribe a través del **único escritor** `StateManager`, que en
**cada** mutación (1) agrega un `EventoHistorial` (campo, valor anterior, valor
nuevo, origen = herramienta, y turno) y (2) **reescribe** el JSON del perfil de
inmediato (escritura atómica vía archivo temporal + `replace`).

**Por qué supera a la extracción posterior:** el estado es consistente turno a
turno (no hay una "pasada final" que pueda olvidar o malinterpretar lo dicho), es
auditable (cada cambio dice qué herramienta lo causó) y es observable en vivo: el
archivo `snapshots/perfil_1082260472.json` **cambia entre turnos** y el
`historial` reconstruye la línea de tiempo (p. ej.
`turno 2 → identidad_validada: False→True (validar_identidad)`).

### Guardrail "identidad antes que deuda"
`consultar_deuda` (y `consultar_planes_pago`) **rechazan** la consulta con
`IDENTIDAD_NO_VALIDADA` mientras `identidad_validada` sea `False`. Es una
defensa real en el backend, no solo una instrucción del prompt: aunque el modelo
intentara saltarse el paso, la herramienta no entrega cifras sensibles.

### Cómo el `historial` prueba "actualización al vuelo"
Al cerrar, `main.py` imprime el `historial` como línea de tiempo. Como cada
entrada lleva el número de turno y el origen, se ve la progresión real de la
gestión; y como el JSON se reescribe en cada mutación, puedes abrir el archivo a
mitad de la conversación y verlo cambiar.

### Robustez
- Argumentos faltantes/sobrantes, herramienta inexistente, enums inválidos,
  monto > saldo, monto no positivo, fecha pasada/mal formada y compromisos
  duplicados → **error estructurado** que el modelo puede leer y corregir.
- Errores de red/rate-limit del API → **reintentos con backoff** y, si persisten,
  mensaje legible conservando el estado parcial.
- `salir`/`exit`/`Ctrl-C`/EOF o entrada vacía → cierre elegante; si la gestión
  quedó a medias se marca `ABANDONADA`. Nunca se filtra un stack trace por fallos
  esperados.

---

## Superficie de herramientas

| Herramienta | Efecto / validación |
|---|---|
| `validar_identidad(documento, nombre_declarado)` | La *herramienta* decide el match (cédula + nombre normalizados). En éxito: `identidad_validada=True`, fija `nombre`. |
| `consultar_deuda(documento)` | Devuelve `{saldo, dias_mora, producto, fecha_corte}`. Bloqueada si la identidad no está validada. Cachea la deuda. |
| `consultar_planes_pago(documento)` | 2–3 planes derivados del saldo (pago único con descuento, 3 y 6 cuotas). |
| `registrar_objecion(documento, tipo, detalle)` | `tipo ∈ TipoObjecion`. |
| `registrar_disposicion(documento, nivel)` | `nivel ∈ DisposicionPago`. |
| `registrar_compromiso_pago(documento, monto, fecha)` | Monto entero positivo ≤ saldo; fecha `YYYY-MM-DD` futura; estampa `referencia_proceso = "CRC-5922"`; evita duplicados. |
| `actualizar_estado_gestion(documento, estado)` | `estado ∈ EstadoGestion`. |

La referencia interna del proceso **`CRC-5922`** (ticket CRC-5922) se define en
`config.PROCESS_REF` y se estampa dentro de `registrar_compromiso_pago`, quedando
en el compromiso persistido.

---

## Ejemplo de transcripción (happy path)

```
================================================================
  Agente de Cobranza Conversacional — Crecere (demo)
================================================================

Agente: Buenas tardes, le habla el área de cobranza. Para poder darle
información, ¿me confirma su nombre completo y su número de cédula, por favor?

Tú: Soy Liliana Ospina Cano, cédula 1.082.260.472
   ↪ [validar_identidad] ✓ {'validado': True, 'nombre': 'Liliana Ospina Cano'}

Agente: Gracias, Liliana, queda validada. Le cuento: su Tarjeta de crédito
presenta un saldo de $5.125.922 con 112 días de mora...
   ↪ [consultar_deuda] ✓ {'saldo': 5125922, 'dias_mora': 112, ...}

Tú: Uy, ahora mismo no tengo plata
   ↪ [registrar_objecion] ✓ {'ok': True}
   ↪ [registrar_disposicion] ✓ {'ok': True}

Agente: Entiendo. Tenemos opciones: pago único con 20% de descuento
($4.100.738), o a 3 o 6 cuotas. ¿Cuál se le acomoda?
   ↪ [consultar_planes_pago] ✓ [{'plan_id': 'PU-20', ...}, ...]

Tú: Puedo abonar 100000 el otro mes
   ↪ [registrar_compromiso_pago] ✓ {'ok': True, 'compromiso': {... 'referencia_proceso': 'CRC-5922'}}
   ↪ [actualizar_estado_gestion] ✓ {'ok': True}

Agente: ¡Perfecto! Queda el compromiso por $100.000 para la fecha acordada.
Gracias, Liliana. Que tenga buen día.

Tú: salir

================================================================
RESUMEN DE LA GESTIÓN
================================================================
Estado de gestión:   COMPROMISO_DE_PAGO
Compromiso:          100000 COP el 2026-07-04 (ref. CRC-5922)

--- Línea de tiempo (historial, actualización al vuelo) ---
  turno 2 → identidad_validada: False→True (validar_identidad)
  turno 2 → nombre: None→Liliana Ospina Cano (validar_identidad)
  turno 2 → deuda: None→{...} (consultar_deuda)
  turno 3 → objeciones: None→{...} (registrar_objecion)
  turno 3 → disposicion_pago: DESCONOCIDA→BAJA (registrar_disposicion)
  turno 4 → compromiso: None→{...} (registrar_compromiso_pago)
  turno 4 → estado_gestion: EN_CURSO→COMPROMISO_DE_PAGO (actualizar_estado_gestion)
================================================================
```

---

## Estructura del proyecto

```
deuda_agent/
  main.py            # consola REPL, wiring, manejo de errores, resumen final
  agent.py           # cliente Gemini, prompt de sistema, bucle manual de tools
  tools.py           # herramientas + esquemas JSON + registro de despacho
  state.py           # DebtorProfile, enums, StateManager (único escritor + JSON)
  data.py            # backend falso en memoria (la "base de datos")
  config.py          # MODEL_NAME, SNAPSHOT_DIR, MAX_VALIDATION_ATTEMPTS, PROCESS_REF
  requirements.txt
  README.md
```
