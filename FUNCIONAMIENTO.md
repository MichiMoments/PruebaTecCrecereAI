# Funcionamiento detallado del Agente de Cobranza Conversacional

> Documento técnico de referencia. Explica el **flujo natural** de la aplicación,
> **todas** las funciones de los seis módulos (qué hacen, qué reciben/devuelven,
> desde dónde se las llama y qué efectos colaterales tienen) y la **mecánica de
> ejecución** ("cuando pasa X, se ejecuta Y, y dónde").
>
> Para instalación, API key y ejemplos de transcripción, ver el
> [README.md](README.md). Este documento lo complementa, no lo reemplaza.

---

## 1. Resumen y propósito

Es un **agente de cobranza por consola**, en **español de Colombia**, construido en
**Python + Google Gemini**. Una corrida del programa equivale a **una sesión de
cobranza sobre un único deudor**: el agente abre la llamada, valida la identidad de
la persona, explica la deuda, propone planes de pago, negocia objeciones, registra
el compromiso de pago y cierra la gestión con un estado final.

El principio rector de todo el diseño es:

> **El modelo (LLM) decide qué herramienta llamar y con qué argumentos; el código
> solo despacha lo que el modelo pide.** No existe una capa de `if` que seleccione
> herramientas.

Mientras transcurre la conversación, el **perfil del deudor** se va actualizando
"al vuelo" (no por una extracción al final) y se **reescribe en disco** en cada
cambio, dejando una **línea de tiempo auditable** (`historial`) de qué herramienta
modificó qué campo y en qué turno.

---

## 2. Arquitectura en capas

El proyecto está separado en **tres capas principales** + **dos capas de soporte**.
El flujo de control y datos va así:

```
                 ┌──────────────────────────────────────────────────────────┐
                 │                       config.py                           │
                 │  Constantes: MODEL_NAME, límites, rutas, PROCESS_REF...   │
                 └──────────────────────────────────────────────────────────┘
                        ▲           ▲            ▲             ▲
                        │ (todas las capas leen constantes de aquí)
                        │
  Usuario (consola)
       │  teclea
       ▼
┌───────────────┐   enviar(texto)   ┌───────────────┐   generate_content   ┌─────────┐
│   main.py     │ ────────────────► │   agent.py    │ ───────────────────► │ Gemini  │
│  (REPL/wiring)│                   │ (orquestación │ ◄─────────────────── │ (nube)  │
│               │ ◄──────────────── │  + prompt)    │   function_calls     └─────────┘
└───────────────┘   respuesta texto └───────────────┘
                                          │  dispatch(nombre, args)
                                          ▼
                                   ┌───────────────┐   métodos de intención   ┌───────────────┐
                                   │   tools.py    │ ───────────────────────► │   state.py    │
                                   │ (8 herramientas│                          │ StateManager  │
                                   │  + esquemas)  │ ◄─────────────────────── │ (único        │
                                   └───────────────┘     (lee profile)        │  escritor)    │
                                          │                                    └───────┬───────┘
                                          │  buscar_deudor / normalizar              │ _persistir()
                                          ▼                                            ▼
                                   ┌───────────────┐                          snapshots/perfil_<doc>.json
                                   │   data.py     │
                                   │ (backend falso│
                                   │  en memoria)  │
                                   └───────────────┘
```

### Responsabilidades (y lo que cada capa NO hace)

| Módulo | Responsabilidad | Lo que NO hace |
|---|---|---|
| [main.py](main.py) | Consola REPL: cablea las capas, corre el bucle, maneja errores y comandos, imprime el resumen final. | No habla con Gemini ni muta el perfil directamente. |
| [agent.py](agent.py) | Cliente Gemini + system prompt + **bucle manual de tool-calling**. Despacha a `tools.py` lo que el modelo pide. | **No lee ni muta el perfil**; **no decide** qué herramienta llamar. |
| [tools.py](tools.py) | Única superficie ejecutable por el LLM: 8 herramientas que validan argumentos y devuelven JSON. Mutan estado **solo** vía `StateManager`. | No decide cuándo llamarse (lo decide el modelo); no escribe el JSON directamente. |
| [state.py](state.py) | Modelo de estado (dataclasses + enums) y `StateManager`, **único escritor**: audita cada cambio e inmediatamente reescribe el JSON. | No conoce el LLM ni las herramientas; no valida reglas de negocio. |
| [data.py](data.py) | Backend falso en memoria (la "base de datos"): deudores semilla + normalización/lookup. | No conoce el LLM, ni el estado, ni la conversación. |
| [config.py](config.py) | Constantes centrales (modelo, límites, rutas, referencias) + carga de `.env`. | No tiene lógica; solo valores. |

---

## 3. El flujo natural, paso a paso

### 3.1 Arranque del programa

Cuando ejecutas `python main.py [cedula]`, ocurre lo siguiente, en orden:

1. **Reconfiguración UTF-8** ([main.py:20-24](main.py#L20-L24)). Antes de importar
   nada pesado, se fuerza `stdout`/`stdin` a UTF-8 para que los acentos y la `ñ` se
   vean bien en la consola de Windows (que suele venir en cp1252).

2. **`main()`** ([main.py:122](main.py#L122)) es el punto de entrada (lo dispara el
   guard `if __name__ == "__main__"` en [main.py:192](main.py#L192)).

3. **Resolver el deudor** con `_resolver_documento(sys.argv)`
   ([main.py:40](main.py#L40)):
   - Sin argumento → `DEFAULT_DOCUMENTO` (`"1082260472"`, Liliana).
   - Con una cédula sembrada (con o sin puntos) → esa cédula normalizada.
   - Con una cédula inexistente → imprime error + lista de deudores disponibles y
     devuelve `None`. Si es `None`, `main()` termina de inmediato
     ([main.py:125-126](main.py#L125-L126)).

4. **Construir el estado**: `state = StateManager(documento, SNAPSHOT_DIR)`
   ([main.py:128](main.py#L128)). El constructor **crea el perfil inicial y ya lo
   persiste en disco** (el JSON existe desde el primer instante).

5. **Construir las herramientas**: `tools = CobranzaTools(state)`
   ([main.py:129](main.py#L129)). Inyecta el `StateManager` y arma el registro
   nombre→función.

6. **Banner de sesión** ([main.py:133-141](main.py#L133-L141)): se busca el deudor
   con `buscar_deudor(documento)` y se imprime cédula + nombre + producto, para que
   quien prueba sepa a quién interpretar. Es información del **lado del gestor**
   (consola), no se le revela al titular sin validar.

7. **Construir el agente**: `agent = CobranzaAgent(tools, documento,
   primer_nombre(deudor["nombre"]), on_tool_call=_trace_tool)`
   ([main.py:144-149](main.py#L144-L149)). Se le pasa **solo el primer nombre** del
   deudor (para saludar, nunca el nombre completo). Aquí se lee la API key, se arma
   el system prompt y la configuración de Gemini. Si falta la API key, lanza
   `AgentError`, que se captura y termina con un mensaje legible.

8. **Turno 1 — apertura forzada** ([main.py:159-163](main.py#L159-L163)): se fija
   `state.turno_actual = 1` y se llama `agent.enviar("[SISTEMA] Inicia tú la
   llamada...")`. Ese mensaje de sistema hace que **el agente hable primero**:
   saluda y pregunta si habla con la persona **usando solo su primer nombre**
   (p. ej. *"¿hablo con Liliana?"*); al confirmar, pide nombre completo y fecha de
   nacimiento para validar. Simula que el gestor inicia la llamada.

### 3.2 El bucle REPL (conversación)

A partir del turno 2, [main.py:160-183](main.py#L160-L183) corre el bucle:

1. `entrada = input("Tú: ").strip()` lee lo que tecleas.
2. **Comandos especiales**:
   - Entrada vacía → reimprime un aviso y continúa ([main.py:167-169](main.py#L167-L169)).
   - `salir` / `exit` / `quit` (set `SALIR`, [main.py:37](main.py#L37)) → rompe el
     bucle ([main.py:170-172](main.py#L170-L172)).
   - `Ctrl-C` / EOF → `KeyboardInterrupt`/`EOFError` capturados → cierre limpio
     ([main.py:163-165](main.py#L163-L165)).
3. Se incrementa `turno` y se fija `state.turno_actual = turno`
   ([main.py:174-175](main.py#L174-L175)). **Este número es el que aparece en el
   `historial`**, por eso se fija ANTES de procesar el turno.
4. `respuesta = agent.enviar(entrada)` ([main.py:177](main.py#L177)) procesa el
   turno completo (incluyendo cuantas herramientas pida el modelo) y devuelve el
   texto final.
5. Si el API falla tras los reintentos, `agent.enviar` lanza `AgentError`, que aquí
   se captura: se avisa, **se conserva el estado parcial** y se rompe el bucle
   ([main.py:178-182](main.py#L178-L182)).
6. Se imprime `Agente: <respuesta>` y se vuelve a esperar entrada.

### 3.3 Qué pasa dentro de `agent.enviar` (bucle manual de tool-calling)

Este es el corazón de la orquestación ([agent.py:173-220](agent.py#L173-L220)):

1. Se **agrega el mensaje del usuario** al historial de la conversación
   (`self._history`) como un `Content` con rol `user`
   ([agent.py:180-182](agent.py#L180-L182)).
2. Se entra a un bucle acotado por `MAX_TOOL_ITERATIONS` (8) para evitar bucles
   infinitos ([agent.py:184](agent.py#L184)).
3. **Se llama al modelo** con `_generar()` ([agent.py:185](agent.py#L185)), que
   reintenta con backoff (ver §4.5).
4. Si la respuesta **no tiene candidatos** (vacía o bloqueada por filtros de
   seguridad), se devuelve un mensaje de disculpa amable y termina el turno
   ([agent.py:187-192](agent.py#L187-L192)).
5. Se agrega la respuesta del modelo al historial
   ([agent.py:194-195](agent.py#L194-L195)).
6. **¿El modelo pidió herramientas?** (`response.function_calls`):
   - **No** → el modelo respondió con texto natural. Se devuelve ese texto y
     **termina el turno** ([agent.py:197-200](agent.py#L197-L200)).
   - **Sí** → por **cada** llamada pedida ([agent.py:203-214](agent.py#L203-L214)):
     - Se ejecuta `self._tools.dispatch(call.name, args)`.
     - Se invoca el callback de traza `on_tool_call` (en consola, `_trace_tool`).
     - El resultado se envuelve con `Part.from_function_response`.
   - Se agregan todos los resultados al historial como un `Content` con rol `tool`
     ([agent.py:214](agent.py#L214)) y **el bucle vuelve al paso 3**: el modelo ya
     ve los resultados y decide el siguiente paso (otra herramienta o texto final).
7. Si se agotan las 8 iteraciones sin que el modelo cierre con texto, se devuelve un
   mensaje de salvaguarda ([agent.py:216-220](agent.py#L216-L220)).

> **Clave del diseño:** la AFC (Automatic Function Calling) del SDK está
> **desactivada** ([agent.py:145-147](agent.py#L145-L147)). Por eso este bucle
> ejecuta las herramientas "a mano": para que sea **explícito y demostrable** que
> es el modelo quien dirige la selección.

### 3.4 Cómo una herramienta muta el estado

Cuando `dispatch` ejecuta una herramienta que cambia algo del perfil, la cadena es
siempre la misma:

```
tools.py (p. ej. consultar_deuda)
   │  llama un MÉTODO DE INTENCIÓN
   ▼
state.py  StateManager.set_deuda(snapshot, "consultar_deuda")
   │  internamente:
   ├─► _registrar(campo, anterior, nuevo, origen)   →  agrega EventoHistorial (con turno + origen)
   │                                                    y luego:
   └─► _persistir()                                  →  reescribe perfil_<doc>.json (tmp + replace atómico)
```

Es decir: **toda** mutación pasa por el **único escritor** (`StateManager`), que en
cada cambio (1) **audita** (agrega un `EventoHistorial` con el turno actual y la
herramienta que lo causó) y (2) **reescribe el JSON completo** de inmediato. Por eso
puedes abrir el archivo `snapshots/perfil_<doc>.json` **a mitad de la conversación**
y verlo cambiar entre turnos.

### 3.5 Diagrama de secuencia de un turno

```
Usuario        main.py            agent.py              Gemini            tools.py          state.py        disco
  │  "Soy        │                   │                    │                  │                 │              │
  │  Liliana"    │                   │                    │                  │                 │              │
  ├─────────────►│ enviar(entrada)   │                    │                  │                 │              │
  │              ├──────────────────►│ append(user)       │                  │                 │              │
  │              │                   ├───────────────────►│ generate_content │                 │              │
  │              │                   │◄───────────────────┤ function_call:   │                 │              │
  │              │                   │  validar_identidad │                 │                 │              │
  │              │                   ├──────────────────────────────────────►│ validar_identidad             │
  │              │                   │                    │                  ├────────────────►│ marcar_..._validada
  │              │                   │                    │                  │                 ├─ _registrar  │
  │              │                   │                    │                  │                 ├─ _persistir ─►│ (JSON reescrito)
  │              │                   │◄──────────────────────────────────────┤ {validado:True} │              │
  │              │                   ├───────────────────►│ (resultado tool) │                 │              │
  │              │                   │◄───────────────────┤ texto natural    │                 │              │
  │              │◄──────────────────┤ return texto       │                  │                 │              │
  │◄─────────────┤ print "Agente:..."│                    │                  │                 │              │
```

### 3.6 Cierre de la sesión

El bloque `finally` de `main()` ([main.py:185-189](main.py#L185-L189)) **siempre**
se ejecuta, salga como salga el bucle:

1. Si la gestión quedó `EN_CURSO` (a medias), se marca `ABANDONADA` con
   `state.set_estado(...)`.
2. Se llama `imprimir_resumen(state)`, que muestra el estado final y la **línea de
   tiempo del `historial`**, demostrando que el perfil se fue actualizando turno a
   turno.

---

## 4. Referencia exhaustiva por archivo y función

### 4.1 `config.py` — constantes centrales

Todos los valores "que es probable cambiar" viven aquí, para que no haya números
mágicos dispersos.

- **Carga de `.env`** ([config.py:15-20](config.py#L15-L20)): intenta importar
  `dotenv` y cargar el `.env` ubicado junto al módulo. Es **opcional**: si
  `python-dotenv` no está instalado, el `except ImportError` lo ignora y se usan las
  variables de entorno del sistema. Esto permite poner `GEMINI_API_KEY` en un `.env`
  sin exportarla a mano.

| Constante | Valor | Para qué sirve / quién la consume |
|---|---|---|
| `MODEL_NAME` | `"gemini-2.5-flash"` | Modelo Gemini a usar. Lo consume `CobranzaAgent._generar` ([agent.py:159](agent.py#L159)). Único punto de cambio del modelo. |
| `API_KEY_ENV` | `"GEMINI_API_KEY"` | Nombre de la variable de entorno con la API key. La lee `CobranzaAgent.__init__` ([agent.py:121](agent.py#L121)). |
| `MAX_API_RETRIES` | `3` | Reintentos ante fallos de red/rate-limit. Lo usa `_generar` ([agent.py:156](agent.py#L156)). |
| `API_BACKOFF_BASE_SECONDS` | `1.5` | Base del backoff entre reintentos (se multiplica por el intento). Lo usa `_generar` ([agent.py:166](agent.py#L166)). |
| `MAX_TOOL_ITERATIONS` | `8` | Tope de iteraciones del bucle de tool-calling por turno. Lo usa `enviar` ([agent.py:184](agent.py#L184)). |
| `SNAPSHOT_DIR` | `<dir>/snapshots` | Carpeta donde se persisten los perfiles. La usa `main()` al crear el `StateManager` ([main.py:128](main.py#L128)). |
| `MAX_VALIDATION_ATTEMPTS` | `3` | Intentos máximos de validación de identidad. Lo usan `validar_identidad` ([tools.py:159](tools.py#L159)) y el system prompt ([agent.py:69](agent.py#L69)). |
| `PROCESS_REF` | `"CRC-5922"` | Referencia interna del proceso; se estampa en todo compromiso. La usa `registrar_compromiso_pago` ([tools.py:346](tools.py#L346)). |
| `COMPANY_NAME` | `"Creceré"` | Identidad del agente; aparece en el system prompt ([agent.py:44](agent.py#L44)). |

### 4.2 `data.py` — backend falso en memoria

Simula el sistema de cartera. No conoce el LLM, ni el estado, ni la conversación:
solo responde consultas de datos crudos.

- **`DebtorRecord`** (TypedDict, [data.py:16-32](data.py#L16-L32)): registro crudo
  de un deudor. El orden de campos refleja **dos bloques**:
  - *Identificación*: `tipo_documento` ("CC"/"CE"), `documento` (cédula
    normalizada), `nombre` (legal completo), `fecha_nacimiento` (`YYYY-MM-DD`,
    factor obligatorio de validación).
  - *Obligación*: `producto`, `saldo` (entero COP), `dias_mora`, `fecha_corte`.

- **`normalizar_documento(documento) -> str`** ([data.py:35-41](data.py#L35-L41)):
  deja **solo los dígitos** (`re.sub(r"\D", "", ...)`). Así `1.082.260.472` y
  `1082260472` resuelven al mismo registro. La usan `buscar_deudor`,
  `_resolver_documento` ([main.py:51](main.py#L51)) y `_documento_de_sesion`
  ([tools.py:110](tools.py#L110)).

- **`normalizar_nombre(nombre) -> str`** ([data.py:44-56](data.py#L44-L56)): pasa a
  minúsculas, **elimina tildes/diacríticos** (vía `unicodedata.normalize("NFD", ...)`
  filtrando categoría `Mn`) y colapsa espacios. Base de la comparación de nombre
  insensible a mayúsculas/acentos. La usa `nombre_coincide`.

- **`primer_nombre(nombre) -> str`** ([data.py:59-66](data.py#L59-L66)): devuelve el
  **primer token** del nombre completo (`""` si está vacío). Sirve para **saludar al
  deudor en la apertura** sin revelar su nombre completo. La usa `main()` al
  construir el agente ([main.py:147](main.py#L147)); el primer nombre llega al system
  prompt, pero el nombre completo **nunca** se le pasa al modelo.

- **`_DEUDORES`** (dict privado, [data.py:66-111](data.py#L66-L111)): los 4 deudores
  semilla, **clave = cédula normalizada**. Productos y rangos de saldo/mora distintos
  para ejercitar la gestión:

  | Cédula | Tipo | Nombre | Producto | Saldo (COP) | Mora | Nacimiento |
  |---|---|---|---|---|---|---|
  | `1082260472` | CC | Liliana Ospina Cano | Tarjeta de crédito | 5 125 922 | 112 d | 1990-05-14 |
  | `71345890` | CC | Carlos Andrés Restrepo Gómez | Crédito de libre inversión | 9 850 000 | 45 d | 1985-11-02 |
  | `1020456789` | CC | María Fernanda Quintero Salazar | Crédito de vehículo | 18 200 000 | 78 d | 1992-08-21 |
  | `1144082356` | CE | Jorge Iván Mejía Loaiza | Crédito educativo | 3 480 000 | 23 d | 1998-03-12 |

- **`buscar_deudor(documento) -> Optional[DebtorRecord]`**
  ([data.py:114-116](data.py#L114-L116)): normaliza la cédula y busca en
  `_DEUDORES`; `None` si no existe. Se la usa en `validar_identidad`,
  `consultar_deuda` ([tools.py:225](tools.py#L225)), `_resolver_documento` y el
  banner de `main()`.

- **`listar_deudores() -> list[DebtorRecord]`**
  ([data.py:119-121](data.py#L119-L121)): devuelve todos los deudores semilla.
  Evita exponer el dict privado. La usa `_resolver_documento` para listar los
  disponibles cuando la cédula no existe ([main.py:57](main.py#L57)).

- **`nombre_coincide(nombre_declarado, record) -> bool`**
  ([data.py:124-126](data.py#L124-L126)): compara el nombre declarado contra el del
  registro, **normalizando ambos**. La usa `validar_identidad`
  ([tools.py:131](tools.py#L131)).

- **`fecha_nacimiento_coincide(declarada, record) -> bool`**
  ([data.py:139-150](data.py#L139-L150)): compara la fecha de nacimiento declarada
  con la del registro (**factor obligatorio** de validación). Parsea ambas fechas
  como `YYYY-MM-DD`; si la declarada no se puede interpretar, devuelve `False`. La
  usa `validar_identidad`.

### 4.3 `state.py` — modelo de estado y único escritor

#### Enums

- **`DisposicionPago`** ([state.py:24-31](state.py#L24-L31)): voluntad de pago
  percibida — `DESCONOCIDA`, `ALTA`, `MEDIA`, `BAJA`, `RENUENTE`.
- **`EstadoGestion`** ([state.py:34-45](state.py#L34-L45)): resultado de la gestión
  — `EN_CURSO` (inicial), `COMPROMISO_DE_PAGO`, `SIN_ACUERDO`,
  `IDENTIDAD_NO_VALIDADA`, `ABANDONADA`, y los casos especiales `NUMERO_EQUIVOCADO`,
  `CONTACTO_TERCERO`, `DEUDA_NO_RECONOCIDA`.
- **`TipoContacto`** ([state.py:48-54](state.py#L48-L54)): con quién se habla —
  `DESCONOCIDO` (inicial), `TITULAR`, `TERCERO`, `NUMERO_EQUIVOCADO`.
- **`TipoObjecion`** ([state.py:57-65](state.py#L57-L65)): categorías de objeción —
  `SIN_LIQUIDEZ`, `NO_RECONOCE_DEUDA`, `YA_PAGO`, `PIDE_TIEMPO`, `DISPUTA_MONTO`,
  `OTRO`.

Todos heredan de `str, Enum`, así que su `.value` es JSON-serializable directamente.

#### Función auxiliar

- **`_now_iso() -> str`** ([state.py:68-70](state.py#L68-L70)): timestamp ISO-8601
  en UTC. Lo usan los dataclasses (campo `ts`) y `_registrar`.

#### Dataclasses

- **`DebtSnapshot`** ([state.py:74-84](state.py#L74-L84)): foto de la deuda
  (`saldo`, `dias_mora`, `producto`, `fecha_corte`) + `to_dict()`. La crea
  `consultar_deuda` ([tools.py:231](tools.py#L231)).
- **`Objecion`** ([state.py:87-96](state.py#L87-L96)): `tipo` (enum), `detalle`,
  `ts` (auto). `to_dict()` serializa el enum a su `.value`. La crea
  `registrar_objecion`.
- **`Compromiso`** ([state.py:99-109](state.py#L99-L109)): `monto`, `fecha`,
  `referencia_proceso`, `ts` (auto) + `to_dict()`. Lo crea
  `registrar_compromiso_pago`.
- **`EventoHistorial`** ([state.py:112-124](state.py#L112-L124)): evento de
  auditoría — `ts`, `turno`, `campo`, `valor_anterior`, `valor_nuevo`, `origen`
  (herramienta que causó el cambio) + `to_dict()`. Lo crea `_registrar`.
- **`DebtorProfile`** ([state.py:127-157](state.py#L127-L157)): **estado completo de
  la gestión**. Campos: `documento`, `nombre`, `identidad_validada`,
  `tipo_contacto`, `nota_contacto`, `deuda`, `disposicion_pago`, `objeciones`
  (lista), `compromiso`, `estado_gestion`, `historial` (lista). Su `to_dict()`
  serializa todo el perfil (incluidos enums → `.value` y sub-objetos → `to_dict`)
  para volcarlo a JSON.

#### `StateManager` — el único escritor

> Ninguna otra capa muta el perfil directamente. Las herramientas llaman **métodos
> de intención** y este gestor audita y persiste cada cambio.

- **`__init__(documento, snapshot_dir)`** ([state.py:169-178](state.py#L169-L178)):
  crea el `DebtorProfile` inicial, asegura la carpeta de snapshots, calcula la ruta
  `perfil_<documento>.json`, inicializa `turno_actual = 0` y **persiste el estado
  inicial** (el archivo existe desde ya).
- **`_registrar(campo, anterior, nuevo, origen)`** (privado,
  [state.py:181-195](state.py#L181-L195)): agrega un `EventoHistorial` (con el
  `turno_actual` y el `origen`) y **llama `_persistir()`**. Es el corazón de la
  auditoría: cada método de intención lo usa.
- **`_persistir()`** (privado, [state.py:197-202](state.py#L197-L202)): vuelca
  `profile.to_dict()` a un archivo temporal `.json.tmp` y luego hace
  `tmp.replace(self._path)` — **escritura atómica** (nunca deja el JSON a medias).
- **`snapshot_path`** (property, [state.py:204-206](state.py#L204-L206)): la ruta
  del JSON. La usa `imprimir_resumen` ([main.py:118](main.py#L118)).

**Métodos de intención** (cada uno registra + persiste vía `_registrar`):

| Método | Quién lo llama | Efecto |
|---|---|---|
| `marcar_identidad_validada(nombre, origen)` ([state.py:209](state.py#L209)) | `validar_identidad` | Pone `identidad_validada=True` y fija `nombre` (registra ambos cambios). |
| `set_deuda(deuda, origen)` ([state.py:219](state.py#L219)) | `consultar_deuda` | Cachea la foto de la deuda. |
| `agregar_objecion(objecion, origen)` ([state.py:225](state.py#L225)) | `registrar_objecion` | Añade una objeción a la lista. |
| `set_disposicion(nivel, origen)` ([state.py:232](state.py#L232)) | `registrar_disposicion` | Actualiza la disposición de pago. |
| `set_contacto(tipo, nota, origen)` ([state.py:238](state.py#L238)) | `validar_identidad`, `registrar_contacto` | Fija `tipo_contacto` y, si hay `nota`, el recado. Registra solo los campos que cambian. |
| `set_compromiso(compromiso, origen)` ([state.py:257](state.py#L257)) | `registrar_compromiso_pago` | Graba el compromiso de pago. |
| `set_estado(estado, origen)` ([state.py:265](state.py#L265)) | `actualizar_estado_gestion`, cierre de `main` | Fija el estado/resultado de la gestión. |

### 4.4 `tools.py` — herramientas, esquemas y despacho

Es la **única superficie** que el LLM puede ejecutar. Cada herramienta valida sus
argumentos y devuelve un objeto JSON-serializable (incluido un error estructurado si
algo falla), de modo que el modelo pueda recuperarse en vez de que el programa se
caiga.

#### Helpers

- **`_error(codigo, mensaje, **extra) -> dict`** ([tools.py:38-40](tools.py#L38-L40)):
  construye un objeto de error uniforme `{"error": codigo, "mensaje": ..., **extra}`.
  Lo usan todas las herramientas para reportar fallos esperables.
- **`_coerce_entero_positivo(valor) -> int | None`**
  ([tools.py:43-51](tools.py#L43-L51)): convierte a entero positivo; **rechaza
  `bool`** (que en Python es subclase de `int`) y devuelve `None` si no es válido.
  Lo usa `registrar_compromiso_pago` para validar el monto.

#### `CobranzaTools`

- **`__init__(state)`** ([tools.py:61-74](tools.py#L61-L74)): guarda el
  `StateManager`, inicializa `validation_attempts = 0` (estado operativo, no del
  perfil) y arma `_registry`: el dict **nombre-de-herramienta → método**, que es lo
  que permite el despacho dinámico.
- **`dispatch(name, args) -> dict`** ([tools.py:77-105](tools.py#L77-L105)): ejecuta
  la herramienta pedida por el modelo. Es la **red de seguridad**:
  - Herramienta inexistente → `_error("HERRAMIENTA_DESCONOCIDA", ...)`.
  - `args` no es dict → `_error("ARGUMENTOS_INVALIDOS", ...)`.
  - `TypeError` (argumentos faltantes/sobrantes) → error estructurado.
  - Cualquier otra excepción → `_error("ERROR_INTERNO", ...)`.
  - **Nunca propaga una excepción a la consola.** La llama
    `CobranzaAgent.enviar` ([agent.py:206](agent.py#L206)).
- **`_documento_de_sesion(documento) -> bool`** ([tools.py:108-110](tools.py#L108-L110)):
  verifica que la cédula recibida (normalizada) corresponda al deudor de **esta**
  sesión (`== self.state.profile.documento`). Es el guardrail de **aislamiento por
  sesión**: aunque el modelo pasara otra cédula, las herramientas no la atienden.

#### Las 8 herramientas

1. **`validar_identidad(documento, nombre_declarado, fecha_nacimiento_declarada)`**
   ([tools.py:113](tools.py#L113)):
   - Si la identidad ya estaba validada, responde `{"validado": True, ...}` sin más.
   - **La fecha de nacimiento es obligatoria.** Si llega vacía, responde
     `_error("FECHA_REQUERIDA", ...)` **sin gastar intento** (es entrada incompleta,
     no un fallo de identidad): el modelo debe pedir la fecha y reintentar.
   - El **match exige las tres condiciones**: cédula de la sesión
     (`_documento_de_sesion`) + nombre (`nombre_coincide`) + fecha
     (`fecha_nacimiento_coincide`).
   - Si todo coincide: llama `marcar_identidad_validada` y `set_contacto(TITULAR)`, y
     devuelve `validado: True` + `nombre` + `tipo_documento`.
   - Si falla (nombre o fecha no coinciden): incrementa `validation_attempts`. Al
     agotar `MAX_VALIDATION_ATTEMPTS` (3), fija
     `EstadoGestion.IDENTIDAD_NO_VALIDADA` y responde `bloqueado: True`. Si quedan
     intentos, responde `validado: False` con `intentos_restantes`.
   - **Es la herramienta —no el modelo— quien decide el match.**

2. **`registrar_objecion(documento, tipo, detalle="")`**
   ([tools.py:167-182](tools.py#L167-L182)): valida que `tipo` ∈ `TipoObjecion`
   (si no, `_error("TIPO_OBJECION_INVALIDO", ...)` con la lista válida) y llama
   `agregar_objecion`.

3. **`registrar_disposicion(documento, nivel)`**
   ([tools.py:184-195](tools.py#L184-L195)): valida `nivel` ∈ `DisposicionPago` y
   llama `set_disposicion`.

4. **`registrar_contacto(documento, tipo_contacto, nota="")`**
   ([tools.py:197-215](tools.py#L197-L215)): pensada para cuando **no** hablamos con
   el titular (`TERCERO`, `NUMERO_EQUIVOCADO`). Valida el enum y llama
   `set_contacto` con la `nota` (recado) opcional.

5. **`consultar_deuda(documento)`** ([tools.py:218-238](tools.py#L218-L238)):
   - **Guardrail**: si `identidad_validada` es `False` →
     `_error("IDENTIDAD_NO_VALIDADA", ...)`. **No entrega cifras sin validar.**
   - Si el documento no es de la sesión o no existe → `_error("NO_ENCONTRADO", ...)`.
   - En caso válido, crea un `DebtSnapshot`, lo cachea con `set_deuda` y lo devuelve.

6. **`consultar_planes_pago(documento)`** ([tools.py:240-290](tools.py#L240-L290)):
   - Requiere identidad validada (mismo guardrail), documento de sesión y que la
     **deuda ya haya sido consultada** (si no, `_error("DEUDA_NO_CONSULTADA", ...)`,
     para no inventar cifras).
   - Deriva **3 planes del saldo**: pago único con 20% de descuento (`PU-20`), 3
     cuotas (`C3`) y 6 cuotas (`C6`). Las cuotas usan **techo de división**
     (`-(-saldo // n)`) para no quedar cortas. Devuelve una **lista** de planes.

7. **`registrar_compromiso_pago(documento, monto, fecha)`**
   ([tools.py:292-349](tools.py#L292-L349)): registra el acuerdo validando, en
   orden:
   - Identidad validada → si no, `IDENTIDAD_NO_VALIDADA`.
   - No exista ya un compromiso → si lo hay, `COMPROMISO_DUPLICADO` (no sobrescribe).
   - La deuda esté consultada → si no, `DEUDA_NO_CONSULTADA`.
   - `monto` entero positivo (`_coerce_entero_positivo`) → si no, `MONTO_INVALIDO`.
   - `monto` ≤ saldo → si no, `MONTO_MAYOR_AL_SALDO`.
   - `fecha` con formato `YYYY-MM-DD` → si no, `FECHA_INVALIDA`.
   - `fecha` estrictamente futura → si no, `FECHA_NO_FUTURA`.
   - Si todo pasa, crea el `Compromiso` **estampando `PROCESS_REF` ("CRC-5922")**,
     lo guarda con `set_compromiso` y devuelve `{"ok": True, "compromiso": {...}}`.

8. **`actualizar_estado_gestion(documento, estado)`**
   ([tools.py:351-364](tools.py#L351-L364)): valida `estado` ∈ `EstadoGestion`
   (si no, `_error("ESTADO_INVALIDO", ...)`) y fija el resultado con `set_estado`.

#### Esquemas para el modelo

- **`_DOC_PROP`** ([tools.py:370-373](tools.py#L370-L373)): el esquema reutilizable
  de la propiedad `documento` (string, "con o sin puntos").
- **`TOOL_SCHEMAS`** ([tools.py:375-535](tools.py#L375-L535)): la **lista de
  esquemas JSON** de las 8 herramientas (nombre, descripción, propiedades,
  `required`). Se declaran como **dicts puros** (sin depender del SDK) para mantener
  `tools.py` desacoplado de `google-genai`. Los `enum` de los esquemas se generan
  directamente de los enums de `state.py` (p. ej. `[t.value for t in TipoObjecion]`),
  así esquema y código nunca se desincronizan. `agent.py` los convierte a
  `FunctionDeclaration`.

### 4.5 `agent.py` — cliente Gemini, prompt y bucle

- **`AgentError(RuntimeError)`** ([agent.py:32-33](agent.py#L32-L33)): error no
  recuperable del agente (API caída tras reintentos, o falta de API key). Lo captura
  `main()`.

- **`construir_system_prompt(documento, primer_nombre) -> str`**
  ([agent.py:36](agent.py#L36)): arma el system prompt. Inyecta la fecha de hoy
  (`date.today()`), el `documento` de la sesión, `COMPANY_NAME` y el **primer
  nombre** del deudor (solo el primer nombre, para saludar). El prompt define:
  - **Identidad**: el agente es el asistente virtual de Creceré; nunca usa
    marcadores de relleno; habla siempre en español de Colombia.
  - **Flujo de 5 pasos** (como *guía*, no transiciones codificadas): (1) **apertura
    saludando por el primer nombre** ("¿hablo con {primer_nombre}?") + validación de
    identidad con nombre completo **y** fecha de nacimiento (ambos obligatorios;
    hasta `MAX_VALIDATION_ATTEMPTS` intentos); (2) contexto de la deuda con
    `consultar_deuda`; (3) propuesta de planes con `consultar_planes_pago`; (4)
    manejo de objeciones (`registrar_objecion` / `registrar_disposicion`); (5)
    cierre con `registrar_compromiso_pago` + `actualizar_estado_gestion`.
  - **Casos especiales**: número equivocado, tercero no titular, y "no reconoce la
    deuda" (solo con titular validado), cada uno con sus herramientas y estado final.
  - **Reglas**: pasar siempre el `documento` de la sesión; no mostrar errores crudos
    ni inventar datos; una pregunta/paso a la vez.
  - **Habeas Data (Ley 1266)**: solo el titular validado recibe información de la
    deuda.

- **`CobranzaAgent.__init__(tools, documento, primer_nombre, on_tool_call=None)`**
  ([agent.py:124](agent.py#L124)):
  - Lee la API key de `API_KEY_ENV`; si falta, lanza `AgentError`.
  - Crea el cliente `genai.Client(api_key=...)`.
  - Convierte `TOOL_SCHEMAS` a `FunctionDeclaration` y arma el
    `GenerateContentConfig` con: el system prompt, las herramientas, y **la AFC
    desactivada** (`AutomaticFunctionCallingConfig(disable=True)`).
  - Inicializa `self._history = []` (la conversación que se reenvía cada turno).

- **`_generar() -> GenerateContentResponse`** (privado,
  [agent.py:153-170](agent.py#L153-L170)): llama
  `client.models.generate_content(model=MODEL_NAME, contents=self._history,
  config=self._config)` con **reintentos** (`MAX_API_RETRIES`) y **backoff** simple
  (`API_BACKOFF_BASE_SECONDS * intento`). Si todos los intentos fallan, lanza
  `AgentError`. La llama `enviar`.

- **`enviar(mensaje_usuario) -> str`** ([agent.py:173-220](agent.py#L173-L220)): el
  **bucle manual de tool-calling** descrito en §3.3. Es el método público que usa
  `main()` en cada turno.

- **`_envolver(resultado) -> dict`** ([agent.py:223-231](agent.py#L223-L231)):
  garantiza que la respuesta de una herramienta sea un **dict** (lo que
  `Part.from_function_response` espera). Si una herramienta devuelve una **lista**
  (como `consultar_planes_pago`), la envuelve bajo la clave `resultado`.

### 4.6 `main.py` — consola, wiring y resumen

- **Preámbulo UTF-8** ([main.py:20-24](main.py#L20-L24)): reconfigura `stdout`/
  `stdin` a UTF-8 (ver §3.1).
- **`DEFAULT_DOCUMENTO`** ([main.py:35](main.py#L35)): cédula del deudor por defecto
  (`"1082260472"`, Liliana) cuando no se pasa argumento.
- **`SALIR`** ([main.py:37](main.py#L37)): set de comandos de salida (`salir`,
  `exit`, `quit`).
- **`_resolver_documento(argv) -> Optional[str]`** ([main.py:40-62](main.py#L40-L62)):
  resuelve qué cédula gestiona la sesión (ver §3.1, paso 3). Devuelve la cédula
  normalizada, el default, o `None` (con error + lista) si la cédula no existe.
- **`_trace_tool(name, args, resultado)`** ([main.py:65-69](main.py#L65-L69)):
  callback que imprime una **traza compacta** de cada herramienta que el modelo
  invoca (con `✓`/`✗` según haya error). Se pasa como `on_tool_call` al agente.
- **`_compacto(valor, limite=160) -> str`** ([main.py:72-75](main.py#L72-L75)):
  acorta un valor para las trazas y la línea de tiempo (recorta con `…`). Lo usan
  `_trace_tool` e `imprimir_resumen`.
- **`imprimir_resumen(state)`** ([main.py:78-119](main.py#L78-L119)): al cerrar,
  imprime el **estado final** del perfil (documento, nombre, identidad, contacto,
  recado, deuda, disposición, objeciones, compromiso, estado) y la **línea de tiempo
  del `historial`** (cada evento con turno, campo, anterior→nuevo y origen), más la
  ruta del JSON persistido.
- **`main()`** ([main.py:122-189](main.py#L122-L189)): el punto de entrada que cablea
  todo y corre el bucle (ver §3.1–§3.6).

---

## 5. Temas transversales

### 5.1 Guardrail "identidad antes que deuda" + Habeas Data (Ley 1266 de 2008)

La no-divulgación a quien no sea el titular validado se garantiza en **dos niveles**:

1. **Backend (defensa real):** `consultar_deuda` y `consultar_planes_pago`
   **rechazan** con `IDENTIDAD_NO_VALIDADA` mientras `identidad_validada` sea
   `False` ([tools.py:220-224](tools.py#L220-L224),
   [tools.py:246-250](tools.py#L246-L250)). Aunque el modelo intentara saltarse el
   paso, la herramienta no entrega cifras.
2. **Prompt (guía):** el system prompt instruye a no invocar esas herramientas ni
   revelar nada ante un tercero o un número equivocado.

### 5.2 Tool-calling manual vs AFC

La AFC del SDK está **desactivada** ([agent.py:145-147](agent.py#L145-L147)) para
ejecutar las herramientas "a mano" en `enviar`. Así es **explícito y demostrable**
que el modelo dirige la selección y no hay ninguna capa de reglas externa.

### 5.3 Actualización "al vuelo" + auditoría

Todo cambio de campo fluye por una herramienta → `StateManager`, que en **cada**
mutación agrega un `EventoHistorial` (campo, anterior, nuevo, origen, turno) y
**reescribe el JSON**. Por eso el estado es consistente turno a turno, auditable
(cada cambio dice qué herramienta lo causó) y observable en vivo. Ejemplo de línea
de tiempo:

```
turno 2 → identidad_validada: False→True (validar_identidad)
turno 2 → nombre: None→Liliana Ospina Cano (validar_identidad)
turno 2 → deuda: None→{...} (consultar_deuda)
turno 3 → objeciones: None→{...} (registrar_objecion)
turno 4 → compromiso: None→{...} (registrar_compromiso_pago)
turno 4 → estado_gestion: EN_CURSO→COMPROMISO_DE_PAGO (actualizar_estado_gestion)
```

### 5.4 Aislamiento por sesión / documento

`_documento_de_sesion` ([tools.py:108-110](tools.py#L108-L110)) hace que las
herramientas solo atiendan la cédula de **esta** sesión. Por eso una sesión validada
para una cédula devuelve `NO_ENCONTRADO` si se le pide la deuda de otra. (Esto
explica por qué probar a Carlos requiere `python main.py 71345890` y no solo
"decir" que eres Carlos en la sesión de Liliana.)

### 5.5 Manejo de errores y robustez

- **Errores esperables** (enum inválido, monto > saldo, fecha pasada, compromiso
  duplicado, herramienta inexistente, argumentos inválidos) → **error
  estructurado** que el modelo lee y corrige; nunca un stack trace.
- **Fallos de red/rate-limit** → reintentos con backoff en `_generar`; si persisten,
  `AgentError` → mensaje legible conservando el estado parcial.
- **Cierre**: `salir`/`exit`/`Ctrl-C`/EOF/entrada vacía se manejan con elegancia; si
  la gestión quedó a medias se marca `ABANDONADA` en el `finally`.

### 5.6 Casos especiales de la llamada

| Situación | Herramientas | Estado / contacto final |
|---|---|---|
| **Número equivocado** | `registrar_contacto(NUMERO_EQUIVOCADO, nota)` → `actualizar_estado_gestion(NUMERO_EQUIVOCADO)` | No gasta intentos de validación; no revela que es una deuda. |
| **Tercero no titular** | `registrar_contacto(TERCERO, nota)` → `actualizar_estado_gestion(CONTACTO_TERCERO)` | No revela datos; deja recado en `nota_contacto`. |
| **No reconoce la deuda** (titular validado) | `registrar_objecion(NO_RECONOCE_DEUDA)`, reafirma con `consultar_deuda`, ajusta disposición → si no se resuelve, `actualizar_estado_gestion(DEUDA_NO_RECONOCIDA)` | Ofrece radicar PQR; no presiona ni inventa. |

### 5.7 Persistencia

El perfil se guarda en `snapshots/perfil_<documento>.json`
([config.py:42](config.py#L42), [state.py:173](state.py#L173)). La escritura es
**atómica**: se escribe a `.json.tmp` y se hace `replace` ([state.py:197-202](state.py#L197-L202)),
de modo que el archivo nunca queda a medias aunque se interrumpa el proceso.

---

## 6. Mapa rápido: "cuando pasa X → se ejecuta Y"

| Disparador | Se ejecuta | Dónde |
|---|---|---|
| Ejecutas `python main.py [cedula]` | `main()` → `_resolver_documento` | [main.py:122](main.py#L122), [main.py:40](main.py#L40) |
| Se crea el `StateManager` | `__init__` → `_persistir` (JSON inicial) | [state.py:169](state.py#L169) |
| El usuario teclea y da Enter | `agent.enviar(entrada)` | [main.py:177](main.py#L177) → [agent.py:173](agent.py#L173) |
| El modelo pide una herramienta | `CobranzaTools.dispatch(name, args)` | [agent.py:206](agent.py#L206) → [tools.py:77](tools.py#L77) |
| El modelo pide `validar_identidad` | `validar_identidad` → `marcar_identidad_validada` + `set_contacto(TITULAR)` | [tools.py:113](tools.py#L113) |
| El modelo pide `consultar_deuda` | `consultar_deuda` → `set_deuda` → JSON | [tools.py:218](tools.py#L218) → [state.py:219](state.py#L219) |
| El modelo pide `registrar_compromiso_pago` | valida monto/fecha → `set_compromiso` (estampa `CRC-5922`) | [tools.py:292](tools.py#L292) |
| Cualquier mutación del perfil | `_registrar` (EventoHistorial) → `_persistir` (atómico) | [state.py:181](state.py#L181) |
| El API falla | `_generar` reintenta con backoff; si persiste, `AgentError` | [agent.py:153](agent.py#L153) |
| Escribes `salir`/`exit`/`Ctrl-C` | rompe el bucle → `finally` → `imprimir_resumen` | [main.py:170](main.py#L170), [main.py:185](main.py#L185) |
| La gestión quedó `EN_CURSO` al cerrar | `set_estado(ABANDONADA)` | [main.py:187-188](main.py#L187-L188) |

---

## 7. Cómo ejecutar y probar (rápido)

```bash
# Deudor por defecto (Liliana):
python main.py

# Otro deudor sembrado (con o sin puntos):
python main.py 71345890        # Carlos Andrés Restrepo Gómez
```

Los 4 deudores de prueba están en la tabla de §4.2. Para **ver la actualización al
vuelo**, abre `snapshots/perfil_<cedula>.json` mientras conversas: cambia entre
turnos. Al cerrar (`salir`), el resumen imprime la **línea de tiempo del
`historial`**.

> Para instalación del entorno, dependencias y configuración de la **API key**
> (`GEMINI_API_KEY` vía `.env`), ver el [README.md](README.md). El programa **no
> arranca sin una API key válida**.
