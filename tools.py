"""Herramientas que el modelo puede invocar + esquemas JSON + registro.

Estas funciones son la *única* superficie que el LLM puede ejecutar. Cada una
valida sus argumentos y devuelve un objeto JSON-serializable (incluido un error
estructurado si algo falla) para que el modelo pueda recuperarse en lugar de
que el programa se caiga.

Las herramientas mutan el estado **solo** a través de ``StateManager``. El
código de orquestación (``agent.py``) jamás decide qué herramienta llamar: solo
despacha lo que el modelo pide. Toda la lógica de "qué hacer" vive aquí o en el
modelo, nunca en una capa de ``if`` que seleccione herramientas.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from config import MAX_VALIDATION_ATTEMPTS, PROCESS_REF
from data import buscar_deudor, nombre_coincide, normalizar_documento
from state import (
    Compromiso,
    DebtSnapshot,
    DisposicionPago,
    EstadoGestion,
    Objecion,
    StateManager,
    TipoContacto,
    TipoObjecion,
)


def _error(codigo: str, mensaje: str, **extra: Any) -> dict[str, Any]:
    """Construye un objeto de error estructurado y uniforme."""
    return {"error": codigo, "mensaje": mensaje, **extra}


def _coerce_entero_positivo(valor: Any) -> int | None:
    """Convierte ``valor`` a entero positivo; ``None`` si no es válido."""
    try:
        if isinstance(valor, bool):  # bool es subclase de int: rechazar
            return None
        entero = int(valor)
    except (TypeError, ValueError):
        return None
    return entero if entero > 0 else None


class CobranzaTools:
    """Implementa las herramientas y mantiene contadores propios del flujo.

    Se inyecta el ``StateManager`` (único escritor). El contador de intentos de
    validación vive aquí porque es estado operativo de la gestión, no del perfil.
    """

    def __init__(self, state: StateManager) -> None:
        self.state = state
        self.validation_attempts = 0
        # Registro nombre-de-herramienta -> implementación.
        self._registry: dict[str, Callable[..., dict[str, Any]]] = {
            "validar_identidad": self.validar_identidad,
            "registrar_contacto": self.registrar_contacto,
            "consultar_deuda": self.consultar_deuda,
            "consultar_planes_pago": self.consultar_planes_pago,
            "registrar_objecion": self.registrar_objecion,
            "registrar_disposicion": self.registrar_disposicion,
            "registrar_compromiso_pago": self.registrar_compromiso_pago,
            "actualizar_estado_gestion": self.actualizar_estado_gestion,
        }

    # -- dispatch -----------------------------------------------------------
    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Ejecuta la herramienta solicitada por el modelo.

        Devuelve siempre un dict JSON-serializable. Errores esperables y bugs
        inesperados se transforman en un objeto de error estructurado para que
        el modelo pueda reaccionar; nunca se propaga una excepción a la consola.
        """
        fn = self._registry.get(name)
        if fn is None:
            return _error(
                "HERRAMIENTA_DESCONOCIDA",
                f"No existe una herramienta llamada '{name}'.",
                herramientas_validas=list(self._registry),
            )
        if not isinstance(args, dict):
            return _error("ARGUMENTOS_INVALIDOS", "Los argumentos deben ser un objeto.")
        try:
            return fn(**args)
        except TypeError as exc:
            # Argumentos faltantes o sobrantes para la firma de la herramienta.
            return _error(
                "ARGUMENTOS_INVALIDOS",
                f"Argumentos inválidos para '{name}': {exc}",
            )
        except Exception as exc:  # red de seguridad: nunca crashear el REPL
            return _error(
                "ERROR_INTERNO",
                f"Error inesperado al ejecutar '{name}': {exc}",
            )

    # -- guardas comunes ----------------------------------------------------
    def _documento_de_sesion(self, documento: str) -> bool:
        """Verifica que ``documento`` corresponda al deudor de esta sesión."""
        return normalizar_documento(documento) == self.state.profile.documento

    # -- herramientas de perfil --------------------------------------------
    def validar_identidad(
        self, documento: str, nombre_declarado: str
    ) -> dict[str, Any]:
        """Valida identidad: la cédula Y el nombre deben coincidir con el registro.

        Es la herramienta —no el modelo— quien decide el match. Tras
        ``MAX_VALIDATION_ATTEMPTS`` fallos, marca la gestión como
        ``IDENTIDAD_NO_VALIDADA`` y reporta ``bloqueado=True`` para que el modelo
        cierre.
        """
        if self.state.profile.identidad_validada:
            return {"validado": True, "mensaje": "La identidad ya estaba validada."}

        record = buscar_deudor(documento)
        coincide = record is not None and nombre_coincide(nombre_declarado, record)

        if coincide and self._documento_de_sesion(documento):
            self.state.marcar_identidad_validada(record["nombre"], "validar_identidad")
            # Una identidad validada implica que hablamos con el titular.
            self.state.set_contacto(TipoContacto.TITULAR, None, "validar_identidad")
            return {"validado": True, "nombre": record["nombre"]}

        # Fallo de validación.
        self.validation_attempts += 1
        restantes = MAX_VALIDATION_ATTEMPTS - self.validation_attempts
        if restantes <= 0:
            self.state.set_estado(
                EstadoGestion.IDENTIDAD_NO_VALIDADA, "validar_identidad"
            )
            return {
                "validado": False,
                "bloqueado": True,
                "intentos_restantes": 0,
                "mensaje": "Se agotaron los intentos de validación. Cerrar la gestión.",
            }
        return {
            "validado": False,
            "bloqueado": False,
            "intentos_restantes": restantes,
            "mensaje": "Los datos no coinciden con nuestros registros.",
        }

    def registrar_objecion(
        self, documento: str, tipo: str, detalle: str = ""
    ) -> dict[str, Any]:
        """Registra una objeción del deudor (categoría + detalle libre)."""
        try:
            tipo_enum = TipoObjecion(tipo)
        except ValueError:
            return _error(
                "TIPO_OBJECION_INVALIDO",
                f"'{tipo}' no es un tipo de objeción válido.",
                tipos_validos=[t.value for t in TipoObjecion],
            )
        self.state.agregar_objecion(
            Objecion(tipo=tipo_enum, detalle=detalle or ""), "registrar_objecion"
        )
        return {"ok": True}

    def registrar_disposicion(self, documento: str, nivel: str) -> dict[str, Any]:
        """Actualiza la disposición/voluntad de pago percibida del deudor."""
        try:
            nivel_enum = DisposicionPago(nivel)
        except ValueError:
            return _error(
                "DISPOSICION_INVALIDA",
                f"'{nivel}' no es una disposición válida.",
                niveles_validos=[d.value for d in DisposicionPago],
            )
        self.state.set_disposicion(nivel_enum, "registrar_disposicion")
        return {"ok": True}

    def registrar_contacto(
        self, documento: str, tipo_contacto: str, nota: str = ""
    ) -> dict[str, Any]:
        """Clasifica con quién se está hablando y opcionalmente guarda un recado.

        Pensada para los casos en que NO hablamos con el titular: ``TERCERO``
        (contestó otra persona) o ``NUMERO_EQUIVOCADO``. ``nota`` permite dejar
        una razón/recado para que el titular se comunique, sin revelar la deuda.
        """
        try:
            tipo_enum = TipoContacto(tipo_contacto)
        except ValueError:
            return _error(
                "TIPO_CONTACTO_INVALIDO",
                f"'{tipo_contacto}' no es un tipo de contacto válido.",
                tipos_validos=[t.value for t in TipoContacto],
            )
        self.state.set_contacto(tipo_enum, nota or None, "registrar_contacto")
        return {"ok": True}

    # -- herramientas de negocio -------------------------------------------
    def consultar_deuda(self, documento: str) -> dict[str, Any]:
        """Devuelve la deuda del deudor. Bloqueada si la identidad no se validó."""
        if not self.state.profile.identidad_validada:
            return _error(
                "IDENTIDAD_NO_VALIDADA",
                "No se puede entregar información de la deuda sin validar identidad.",
            )
        record = buscar_deudor(documento)
        if record is None or not self._documento_de_sesion(documento):
            return _error(
                "NO_ENCONTRADO",
                f"No se encontró ninguna obligación para el documento '{documento}'.",
            )
        snapshot = DebtSnapshot(
            saldo=record["saldo"],
            dias_mora=record["dias_mora"],
            producto=record["producto"],
            fecha_corte=record["fecha_corte"],
        )
        self.state.set_deuda(snapshot, "consultar_deuda")
        return snapshot.to_dict()

    def consultar_planes_pago(self, documento: str) -> dict[str, Any] | list[dict[str, Any]]:
        """Devuelve 2–3 planes de pago derivados del saldo de la deuda.

        Requiere identidad validada y que la deuda haya sido consultada antes
        (para no inventar cifras).
        """
        if not self.state.profile.identidad_validada:
            return _error(
                "IDENTIDAD_NO_VALIDADA",
                "No se pueden ofrecer planes sin validar identidad.",
            )
        if not self._documento_de_sesion(documento):
            return _error(
                "NO_ENCONTRADO",
                f"No se encontró ninguna obligación para el documento '{documento}'.",
            )
        deuda = self.state.profile.deuda
        if deuda is None:
            return _error(
                "DEUDA_NO_CONSULTADA",
                "Primero debe consultarse la deuda con consultar_deuda.",
            )

        saldo = deuda.saldo
        descuento_unico = round(saldo * 0.80)  # 20% de descuento por pago único
        cuota_3 = -(-saldo // 3)  # techo de la división para no quedar corto
        cuota_6 = -(-saldo // 6)
        planes = [
            {
                "plan_id": "PU-20",
                "descripcion": "Pago único con 20% de descuento",
                "num_cuotas": 1,
                "valor_cuota": descuento_unico,
                "total": descuento_unico,
            },
            {
                "plan_id": "C3",
                "descripcion": "3 cuotas mensuales sin descuento",
                "num_cuotas": 3,
                "valor_cuota": cuota_3,
                "total": cuota_3 * 3,
            },
            {
                "plan_id": "C6",
                "descripcion": "6 cuotas mensuales sin descuento",
                "num_cuotas": 6,
                "valor_cuota": cuota_6,
                "total": cuota_6 * 6,
            },
        ]
        return planes

    def registrar_compromiso_pago(
        self, documento: str, monto: Any, fecha: str
    ) -> dict[str, Any]:
        """Registra un compromiso de pago validando monto y fecha.

        - ``monto``: entero positivo ≤ saldo.
        - ``fecha``: ``YYYY-MM-DD`` y estrictamente futura.
        Estampa la constante ``referencia_proceso = "CRC-5922"`` (ticket
        CRC-5922) en el compromiso persistido. Evita sobrescribir uno existente.
        """
        if not self.state.profile.identidad_validada:
            return _error(
                "IDENTIDAD_NO_VALIDADA",
                "No se puede registrar compromiso sin validar identidad.",
            )
        if self.state.profile.compromiso is not None:
            return _error(
                "COMPROMISO_DUPLICADO",
                "Ya existe un compromiso registrado para esta gestión.",
                compromiso_actual=self.state.profile.compromiso.to_dict(),
            )
        deuda = self.state.profile.deuda
        if deuda is None:
            return _error(
                "DEUDA_NO_CONSULTADA",
                "Primero debe consultarse la deuda con consultar_deuda.",
            )

        monto_int = _coerce_entero_positivo(monto)
        if monto_int is None:
            return _error(
                "MONTO_INVALIDO", "El monto debe ser un entero positivo en COP."
            )
        if monto_int > deuda.saldo:
            return _error(
                "MONTO_MAYOR_AL_SALDO",
                f"El monto ({monto_int}) no puede superar el saldo ({deuda.saldo}).",
                saldo=deuda.saldo,
            )

        try:
            fecha_dt = datetime.strptime(str(fecha), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return _error(
                "FECHA_INVALIDA", "La fecha debe tener el formato YYYY-MM-DD."
            )
        if fecha_dt <= date.today():
            return _error(
                "FECHA_NO_FUTURA", "La fecha de pago debe ser futura."
            )

        compromiso = Compromiso(
            monto=monto_int,
            fecha=fecha_dt.isoformat(),
            referencia_proceso=PROCESS_REF,  # ticket interno CRC-5922
        )
        self.state.set_compromiso(compromiso, "registrar_compromiso_pago")
        return {"ok": True, "compromiso": compromiso.to_dict()}

    def actualizar_estado_gestion(
        self, documento: str, estado: str
    ) -> dict[str, Any]:
        """Fija el resultado final de la gestión (valor de ``EstadoGestion``)."""
        try:
            estado_enum = EstadoGestion(estado)
        except ValueError:
            return _error(
                "ESTADO_INVALIDO",
                f"'{estado}' no es un estado de gestión válido.",
                estados_validos=[e.value for e in EstadoGestion],
            )
        self.state.set_estado(estado_enum, "actualizar_estado_gestion")
        return {"ok": True}


# --- Esquemas JSON de las herramientas (los consume el agente) -------------
# Se declaran como dicts puros (sin depender del SDK) para mantener tools.py
# desacoplado de google-genai. agent.py los convierte a FunctionDeclaration.
_DOC_PROP = {
    "type": "string",
    "description": "Cédula del deudor (se acepta con o sin puntos).",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "validar_identidad",
        "description": (
            "Valida la identidad del deudor. La cédula Y el nombre declarado "
            "deben coincidir con el registro. Llamar ANTES de revelar cualquier "
            "dato de la deuda."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documento": _DOC_PROP,
                "nombre_declarado": {
                    "type": "string",
                    "description": "Nombre completo que declara la persona.",
                },
            },
            "required": ["documento", "nombre_declarado"],
        },
    },
    {
        "name": "consultar_deuda",
        "description": (
            "Devuelve saldo, días de mora, producto y fecha de corte. Falla con "
            "IDENTIDAD_NO_VALIDADA si la identidad no ha sido validada."
        ),
        "parameters": {
            "type": "object",
            "properties": {"documento": _DOC_PROP},
            "required": ["documento"],
        },
    },
    {
        "name": "consultar_planes_pago",
        "description": (
            "Devuelve 2–3 planes de pago derivados del saldo. Requiere identidad "
            "validada y haber consultado la deuda. Ofrecer SOLO estos planes."
        ),
        "parameters": {
            "type": "object",
            "properties": {"documento": _DOC_PROP},
            "required": ["documento"],
        },
    },
    {
        "name": "registrar_objecion",
        "description": "Registra una objeción del deudor cuando aparezca.",
        "parameters": {
            "type": "object",
            "properties": {
                "documento": _DOC_PROP,
                "tipo": {
                    "type": "string",
                    "enum": [t.value for t in TipoObjecion],
                    "description": "Categoría de la objeción.",
                },
                "detalle": {
                    "type": "string",
                    "description": "Texto libre con el detalle de la objeción.",
                },
            },
            "required": ["documento", "tipo"],
        },
    },
    {
        "name": "registrar_disposicion",
        "description": (
            "Actualiza la disposición/voluntad de pago percibida a medida que se "
            "aclara durante la conversación."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documento": _DOC_PROP,
                "nivel": {
                    "type": "string",
                    "enum": [d.value for d in DisposicionPago],
                    "description": "Nivel de disposición de pago.",
                },
            },
            "required": ["documento", "nivel"],
        },
    },
    {
        "name": "registrar_contacto",
        "description": (
            "Clasifica con quién se habla cuando NO es el titular validado: "
            "TERCERO (contestó otra persona) o NUMERO_EQUIVOCADO. Usa `nota` para "
            "dejar un recado para que el titular se comunique, sin revelar la deuda."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documento": _DOC_PROP,
                "tipo_contacto": {
                    "type": "string",
                    "enum": ["TERCERO", "NUMERO_EQUIVOCADO", "TITULAR"],
                    "description": "Con quién se está hablando.",
                },
                "nota": {
                    "type": "string",
                    "description": "Recado para el titular (opcional, sin datos de la deuda).",
                },
            },
            "required": ["documento", "tipo_contacto"],
        },
    },
    {
        "name": "registrar_compromiso_pago",
        "description": (
            "Registra el compromiso de pago. monto: entero positivo ≤ saldo; "
            "fecha: YYYY-MM-DD futura. Estampa la referencia de proceso."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documento": _DOC_PROP,
                "monto": {
                    "type": "integer",
                    "description": "Monto a pagar en COP (entero, ≤ saldo).",
                },
                "fecha": {
                    "type": "string",
                    "description": "Fecha de pago en formato YYYY-MM-DD (futura).",
                },
            },
            "required": ["documento", "monto", "fecha"],
        },
    },
    {
        "name": "actualizar_estado_gestion",
        "description": (
            "Fija el resultado final de la gestión. Usa: COMPROMISO_DE_PAGO (hay "
            "acuerdo), SIN_ACUERDO (sin acuerdo de pago), IDENTIDAD_NO_VALIDADA "
            "(no se pudo validar), NUMERO_EQUIVOCADO (el número no es del titular), "
            "CONTACTO_TERCERO (contestó un tercero), DEUDA_NO_RECONOCIDA (el titular "
            "niega la deuda sin resolverse)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documento": _DOC_PROP,
                "estado": {
                    "type": "string",
                    "enum": [e.value for e in EstadoGestion],
                    "description": "Estado final de la gestión.",
                },
            },
            "required": ["documento", "estado"],
        },
    },
]
