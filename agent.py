"""Cliente Gemini, prompt de sistema y bucle manual de tool-calling.

Esta capa NO conoce el perfil ni lo muta: solo (1) mantiene la conversación con
Gemini, (2) recibe las llamadas a herramientas que decide el modelo y (3) las
despacha al registro de ``tools.py``, devolviendo el resultado al modelo. No hay
ninguna capa de ``if`` que seleccione herramientas: el modelo es quien elige
qué llamar y con qué argumentos.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Callable, Optional

from google import genai
from google.genai import types

from config import (
    API_BACKOFF_BASE_SECONDS,
    API_KEY_ENV,
    MAX_API_RETRIES,
    MAX_TOOL_ITERATIONS,
    MAX_VALIDATION_ATTEMPTS,
    MODEL_NAME,
)
from tools import TOOL_SCHEMAS, CobranzaTools


class AgentError(RuntimeError):
    """Error no recuperable del agente (p. ej. API caída tras reintentos)."""


def construir_system_prompt(documento: str) -> str:
    """Arma el prompt de sistema con la guía del flujo de cobranza.

    El flujo va como *guía*, no como transiciones de estado codificadas: el
    modelo decide cuándo llamar cada herramienta.
    """
    #TODO:
    hoy = date.today().isoformat()
    return f"""\
Eres un agente de cobranza (cobranza) de una entidad financiera colombiana.
Hablas SIEMPRE en español de Colombia: cálido pero profesional, claro y conciso.
La fecha de hoy es {hoy}. El documento en gestión de esta sesión es {documento}.

OBJETIVO: gestionar el cobro de una obligación en mora, llegando idealmente a un
compromiso de pago concreto (monto + fecha).

FLUJO (es una guía, tú decides el momento de cada paso):
1) Saludo y VALIDACIÓN DE IDENTIDAD. Pide nombre completo y confírmalo con la
   herramienta `validar_identidad`. NO reveles ningún dato de la deuda hasta que
   la identidad esté validada. Tienes hasta {MAX_VALIDATION_ATTEMPTS} intentos;
   si se agotan (la herramienta responde bloqueado=true), discúlpate y cierra
   llamando `actualizar_estado_gestion` con estado IDENTIDAD_NO_VALIDADA.
2) CONTEXTO DE LA DEUDA. Usa `consultar_deuda` y explica saldo, días de mora y
   producto. Usa SOLO las cifras que devuelva la herramienta; nunca inventes
   saldo, mora ni montos.
3) PROPUESTA DE PLANES. Usa `consultar_planes_pago` y ofrece ÚNICAMENTE los
   planes que devuelva. No inventes planes ni descuentos.
4) MANEJO DE OBJECIONES. Negocia de verdad las objeciones ("no tengo plata", "no
   reconozco la deuda", "ya pagué", "llámenme después", "el monto está mal",
   etc.). Cada vez que aparezca una objeción, regístrala con `registrar_objecion`.
   A medida que se aclare la voluntad de pago, actualízala con
   `registrar_disposicion`.
5) CIERRE. Si se llega a un acuerdo, registra el compromiso con
   `registrar_compromiso_pago` (monto entero ≤ saldo y fecha YYYY-MM-DD futura) y
   luego `actualizar_estado_gestion` con COMPROMISO_DE_PAGO. Si no hay acuerdo,
   usa SIN_ACUERDO.

REGLAS:
- Pasa siempre el documento {documento} como argumento `documento` de las
  herramientas.
- Si una herramienta devuelve un objeto con "error", explica el problema con tus
  palabras y reintenta o pide la información que falte; nunca muestres el error
  crudo ni te inventes los datos.
- Una sola pregunta o paso a la vez; respuestas breves y naturales, como en una
  llamada telefónica real.
"""


class CobranzaAgent:
    """Orquesta la conversación con Gemini y el despacho de herramientas."""

    def __init__(
        self,
        tools: CobranzaTools,
        documento: str,
        on_tool_call: Optional[Callable[[str, dict[str, Any], Any], None]] = None,
    ) -> None:
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise AgentError(
                f"No se encontró la variable de entorno {API_KEY_ENV}. "
                "Configura tu API key de Google AI Studio."
            )
        self._client = genai.Client(api_key=api_key)
        self._tools = tools
        self._on_tool_call = on_tool_call

        # Declaración de herramientas para el modelo (esquemas -> SDK).
        function_declarations = [
            types.FunctionDeclaration(
                name=schema["name"],
                description=schema["description"],
                parameters_json_schema=schema["parameters"],
            )
            for schema in TOOL_SCHEMAS
        ]
        self._config = types.GenerateContentConfig(
            system_instruction=construir_system_prompt(documento),
            tools=[types.Tool(function_declarations=function_declarations)],
            # Bucle MANUAL: desactivamos la AFC para ejecutar las herramientas
            # nosotros mismos y demostrar que el modelo dirige la selección.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        # Historial de la conversación (lo que se reenvía en cada turno).
        self._history: list[types.Content] = []

    # -- API con reintentos -------------------------------------------------
    def _generar(self) -> types.GenerateContentResponse:
        """Llama al modelo con reintentos y backoff exponencial simple."""
        ultimo_error: Optional[Exception] = None
        for intento in range(1, MAX_API_RETRIES + 1):
            try:
                return self._client.models.generate_content(
                    model=MODEL_NAME,
                    contents=self._history,
                    config=self._config,
                )
            except Exception as exc:  # red/rate-limit/servidor
                ultimo_error = exc
                if intento < MAX_API_RETRIES:
                    time.sleep(API_BACKOFF_BASE_SECONDS * intento)
        raise AgentError(
            f"No fue posible contactar el modelo tras {MAX_API_RETRIES} intentos: "
            f"{ultimo_error}"
        )

    # -- turno conversacional ----------------------------------------------
    def enviar(self, mensaje_usuario: str) -> str:
        """Procesa un turno del usuario y devuelve la respuesta en texto.

        Implementa el bucle manual de tool-calling: mientras el modelo pida
        herramientas, las ejecuta y le devuelve los resultados; termina cuando
        el modelo responde con texto natural.
        """
        self._history.append(
            types.Content(role="user", parts=[types.Part.from_text(text=mensaje_usuario)])
        )

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self._generar()

            if not response.candidates:
                # Respuesta vacía o bloqueada por filtros de seguridad.
                return (
                    "Disculpa, tuve un inconveniente para procesar eso. "
                    "¿Podrías repetírmelo, por favor?"
                )

            model_content = response.candidates[0].content
            self._history.append(model_content)

            function_calls = response.function_calls or []
            if not function_calls:
                # El modelo respondió con texto: fin del turno.
                return (response.text or "").strip() or "¿Podrías repetirme, por favor?"

            # El modelo pidió una o más herramientas: las ejecutamos.
            tool_parts: list[types.Part] = []
            for call in function_calls:
                args = dict(call.args or {})
                resultado = self._tools.dispatch(call.name, args)
                if self._on_tool_call is not None:
                    self._on_tool_call(call.name, args, resultado)
                tool_parts.append(
                    types.Part.from_function_response(
                        name=call.name, response=_envolver(resultado)
                    )
                )
            self._history.append(types.Content(role="tool", parts=tool_parts))

        # Salvaguarda anti-bucle infinito.
        return (
            "Estoy teniendo dificultades para completar la gestión en este momento. "
            "¿Podemos retomar el último punto?"
        )


def _envolver(resultado: Any) -> dict[str, Any]:
    """Garantiza que la respuesta de la herramienta sea un objeto JSON.

    ``Part.from_function_response`` espera un dict; si una herramienta devuelve
    una lista (p. ej. los planes), se envuelve bajo la clave ``resultado``.
    """
    if isinstance(resultado, dict):
        return resultado
    return {"resultado": resultado}
