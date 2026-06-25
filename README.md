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


## Supuestos (implementados y asumidos)

El deudor se modela como un registro plano (`DebtorRecord`, en `data.py`), ordenado en dos bloques: la identificación (`tipo_documento`, `documento`, `nombre`, `fecha_nacimiento`) y la obligación (`producto`, `saldo`, `dias_mora`, `fecha_corte`).

Para las pruebas dejé cuatro deudores cargados en `data.py`, con productos y rangos de saldo y mora distintos para poder ejercitar toda la gestión. El primero es Liliana Ospina Cano (cédula 1.082.260.472, CC), con una tarjeta de crédito de $5.125.922 y 112 días de mora; este es el deudor de la sesión por defecto. Los otros tres son Carlos Andrés Restrepo Gómez (71.345.890, CC), con un crédito de libre inversión de $9.850.000 y 45 días de mora; María Fernanda Quintero Salazar (1.020.456.789, CC), con un crédito de vehículo de $18.200.000 y 78 días de mora; y Jorge Iván Mejía Loaiza (1.144.082.356, CE), con un crédito educativo de $3.480.000 y 23 días de mora. Para gestionar un deudor distinto al de la sesión por defecto se ejecuta `python main.py <cédula>`, con o sin puntos.

La cédula se normaliza ignorando puntos y espacios, así que `1082260472` y `1.082.260.472` resuelven al mismo registro.

Como la sesión ya sabe a qué deudor corresponde la línea, el agente abre la llamada saludando y preguntando si habla con la persona usando solo su primer nombre (por ejemplo, "¿hablo con Liliana?"). No revela el nombre completo ni nada de la deuda antes de validar; al prompt solo se le pasa el primer nombre, nunca el completo.

La identidad se considera válida solo si coinciden la cédula, el nombre completo y la fecha de nacimiento con el registro. La comparación del nombre es insensible a mayúsculas y a tildes. La fecha de nacimiento es obligatoria y va en formato `YYYY-MM-DD`: si la persona todavía no la da, la herramienta la pide con `FECHA_REQUERIDA` sin gastar intento; si la da y no coincide, eso sí cuenta como fallo. Después de tres intentos fallidos (`MAX_VALIDATION_ATTEMPTS = 3`) la gestión pasa a `IDENTIDAD_NO_VALIDADA` y se cierra.

Si la cédula no existe o viene mal formada, la herramienta devuelve un error estructurado `NO_ENCONTRADO`, de modo que esa rama también queda ejercitable.

El dinero se maneja como entero en COP y las fechas en formato `YYYY-MM-DD`. Un compromiso de pago tiene que quedar en una fecha futura y por un monto menor o igual al saldo.

Cada corrida es una sesión de un solo deudor, que se persiste en su propio archivo JSON. Por defecto se gestiona la obligación de la cédula 1082260472, y el agente abre la llamada pidiendo la validación de identidad.

## Decisiones de arquitectura (y por qué)

### Separación en tres capas

Separé el código en tres capas. `agent.py` es el modelo y la orquestación: habla con Gemini, mantiene el historial de la conversación y corre el bucle manual de tool-calling; no lee ni modifica el perfil, solo despacha herramientas. `tools.py` son las herramientas, que son la única superficie que el modelo puede ejecutar; cada una valida sus argumentos y devuelve un objeto serializable en JSON (incluido el error estructurado cuando algo sale mal), y mutan el estado únicamente a través del `StateManager`. `state.py` es el estado: las dataclasses y enums del perfil, más un `StateManager` que es el único componente que escribe, audita cada cambio en el historial y reescribe el JSON en disco. Como apoyo están `data.py`, que es el backend falso en memoria (la "base de datos"), y `config.py`, donde tengo las constantes.

### El modelo, y no un if, elige las herramientas

Quise que fuera el modelo, y no un `if` mío, el que decida qué herramienta usar. El bucle de `agent.py` declara los esquemas, manda el mensaje y, si la respuesta trae llamadas en `function_calls`, ejecuta exactamente las que el modelo pidió (`CobranzaTools.dispatch`) y le devuelve los resultados con `Part.from_function_response`; eso se repite hasta que el modelo contesta en texto. Uso function-calling manual, con `AutomaticFunctionCallingConfig(disable=True)`, en vez del AFC automático del SDK, justamente para que quede explícito y demostrable que es el modelo el que dirige la selección de herramientas. No hay ninguna capa de reglas externa que decida qué llamar.

### Mecanismo híbrido de actualización al vuelo

El perfil tiene que actualizarse durante la conversación, no al final. Para lograrlo sin una capa de reglas aparte, hice que todo cambio de campo pase por una herramienta. Las herramientas de negocio que de todos modos se necesitan tienen efectos sobre el perfil: `consultar_deuda` cachea la deuda, `registrar_compromiso_pago` graba el compromiso y `actualizar_estado_gestion` fija el estado. Además agregué herramientas de perfil más pequeñas (`validar_identidad`, `registrar_objecion`, `registrar_disposicion`) para que también los campos no comerciales, como la identidad, las objeciones y la disposición, se actualicen por una llamada del modelo y no por una extracción posterior.

Cada herramienta escribe a través del único escritor, el `StateManager`, que en cada mutación hace dos cosas: agrega un `EventoHistorial` (campo, valor anterior, valor nuevo, origen de la herramienta y turno) y reescribe de una vez el JSON del perfil, con una escritura atómica usando un archivo temporal y `replace`.

Esto le gana a la extracción posterior por varias razones. El estado queda consistente turno a turno, sin depender de una pasada final que pueda olvidar o malinterpretar lo que se dijo. Queda auditable, porque cada cambio dice qué herramienta lo causó. Y es observable en vivo: el archivo `snapshots/perfil_1082260472.json` cambia entre turnos, y el historial reconstruye la línea de tiempo (por ejemplo, turno 2 → `identidad_validada: False→True` por `validar_identidad`).

### Guardrail de identidad antes que deuda

`consultar_deuda` y `consultar_planes_pago` rechazan la consulta con `IDENTIDAD_NO_VALIDADA` mientras `identidad_validada` esté en `False`. Es una defensa real en el backend, no solo una instrucción del prompt: aunque el modelo intentara saltarse el paso, la herramienta no entrega cifras sensibles.

### Cómo el historial prueba la actualización al vuelo

Al cerrar, `main.py` imprime el historial como una línea de tiempo. Como cada entrada lleva el número de turno y el origen, se ve la progresión real de la gestión; y como el JSON se reescribe en cada mutación, se puede abrir el archivo a mitad de la conversación y verlo cambiar.

### Robustez

Para los errores previsibles, la respuesta es siempre un error estructurado que el modelo puede leer y corregir: argumentos faltantes o de más, una herramienta que no existe, enums inválidos, un monto mayor al saldo o no positivo, una fecha pasada o mal formada, y compromisos duplicados. Ante errores de red o de rate-limit del API hay reintentos con backoff y, si el problema persiste, un mensaje legible que conserva el estado parcial. Si la persona escribe `salir` o `exit`, manda Ctrl-C o EOF, o deja la entrada vacía, la sesión cierra de forma elegante; si la gestión quedó a medias se marca como `ABANDONADA`. Nunca se filtra un stack trace por un fallo esperado.

### Casos especiales de la llamada (con quién hablo y protección de datos)

Hay tres situaciones reales de cobranza que manejo con guía en el system prompt, no con reglas en código, y dejando el desenlace observable en el estado.

La primera es que la persona no reconozca la deuda, y esto solo aplica con el titular ya validado: el agente registra `registrar_objecion(NO_RECONOCE_DEUDA)`, reafirma únicamente con las cifras de `consultar_deuda`, ofrece radicar una reclamación o PQR y ajusta la disposición; si no se resuelve, cierra en `DEUDA_NO_RECONOCIDA`.

La segunda es que sea un número equivocado: el agente se disculpa sin mencionar que se trata de una deuda, llama `registrar_contacto(NUMERO_EQUIVOCADO, nota)` y cierra en `NUMERO_EQUIVOCADO`, sin gastar intentos de validación.

La tercera es que conteste un tercero que no es el titular: ahí no se revela ningún dato, se ofrece dejar un recado para que el titular se comunique, se llama `registrar_contacto(TERCERO, nota)` y se cierra en `CONTACTO_TERCERO`.

Sobre la protección de datos (Habeas Data, Ley 1266 de 2008): la no divulgación a quien no sea el titular validado la garantizo en dos niveles. Uno es el guardrail de `consultar_deuda` y `consultar_planes_pago`, que rechazan con `IDENTIDAD_NO_VALIDADA` mientras `identidad_validada` esté en `False`, lo cual es una defensa real en el backend y no solo en el prompt. El otro es la guía del prompt para no invocar esas herramientas ni revelar nada ante un tercero o un número equivocado. El enum `TipoContacto` (`DESCONOCIDO`, `TITULAR`, `TERCERO`, `NUMERO_EQUIVOCADO`) deja registrado con quién se habló, y el recado queda en `nota_contacto`.

## Superficie de herramientas

Estas son las herramientas que el modelo puede llamar.

`validar_identidad(documento, nombre_declarado, fecha_nacimiento_declarada)`: la herramienta es la que decide el match; deben coincidir cédula, nombre y fecha de nacimiento, todos obligatorios, y si falta la fecha responde `FECHA_REQUERIDA` sin gastar intento. En éxito deja `identidad_validada` en `True`, fija el nombre, devuelve el `tipo_documento` y marca `tipo_contacto` como `TITULAR`.

`registrar_contacto(documento, tipo_contacto, nota)`: clasifica con quién se habla cuando no es el titular; `tipo_contacto` sale de `TipoContacto` (`TERCERO`, `NUMERO_EQUIVOCADO`, `TITULAR`) y la `nota` es opcional, pensada como recado para el titular, sin datos de la deuda.

`consultar_deuda(documento)`: devuelve saldo, días de mora, producto y fecha de corte; está bloqueada si la identidad no está validada y cachea la deuda.

`consultar_planes_pago(documento)`: arma dos o tres planes a partir del saldo (pago único con descuento, y a 3 y 6 cuotas).

`registrar_objecion(documento, tipo, detalle)`: el `tipo` sale de `TipoObjecion`.

`registrar_disposicion(documento, nivel)`: el `nivel` sale de `DisposicionPago`.

`registrar_compromiso_pago(documento, monto, fecha)`: el monto debe ser un entero positivo menor o igual al saldo y la fecha `YYYY-MM-DD` futura; estampa la referencia de proceso `CRC-5922` y evita duplicados.

`actualizar_estado_gestion(documento, estado)`: el `estado` sale de `EstadoGestion`, que incluye `NUMERO_EQUIVOCADO`, `CONTACTO_TERCERO` y `DEUDA_NO_RECONOCIDA`.

La referencia interna del proceso, `CRC-5922`, la defino en `config.PROCESS_REF` y se estampa dentro de `registrar_compromiso_pago`, quedando guardada en el compromiso.

## Ejemplo de transcripción (happy path)

```
================================================================
  Agente de Cobranza Conversacional — Crecere (demo)
================================================================

Agente: Buenas tardes, le habla el asistente virtual de Creceré. ¿Hablo con
Liliana, por favor?

Tú: Sí, con ella

Agente: Gracias. Para confirmar su identidad, ¿me regala su nombre completo?

Tú: Liliana Ospina Cano

Agente: Perfecto. Y por seguridad, ¿su fecha de nacimiento?

Tú: 14 de mayo de 1990
   ↪ [validar_identidad] ✓ {'validado': True, 'nombre': 'Liliana Ospina Cano', 'tipo_documento': 'CC'}

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

## Caso especial: contesta un tercero (sin divulgar datos)

```
Agente: Buenas tardes, ¿hablo con Liliana, por favor?

Tú: No, soy el esposo. Ella no se encuentra ahora.
   ↪ [registrar_contacto] ✓ {'ok': True}

Agente: Entiendo, gracias. Es un asunto personal de Liliana; ¿sería tan amable
de pedirle que se comunique con nuestra línea de atención cuando pueda?
   ↪ [actualizar_estado_gestion] ✓ {'ok': True}

Tú: salir

================================================================
RESUMEN DE LA GESTIÓN
================================================================
Identidad validada:  False
Tipo de contacto:    TERCERO
Recado:              Pedir a la titular que devuelva la llamada a la línea de atención
Estado de gestión:   CONTACTO_TERCERO

--- Línea de tiempo (historial, actualización al vuelo) ---
  turno 1 → tipo_contacto: DESCONOCIDO→TERCERO (registrar_contacto)
  turno 1 → nota_contacto: None→Pedir a la titular que devuelva... (registrar_contacto)
  turno 1 → estado_gestion: EN_CURSO→CONTACTO_TERCERO (actualizar_estado_gestion)
================================================================
```

Acá vale la pena notar que nunca se llamó `consultar_deuda` ni se mencionó el saldo o el producto: a un tercero no se le divulga nada.

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


Link video yt: https://youtu.be/XZhodhAwBe8