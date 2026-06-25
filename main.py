"""Consola (REPL) del agente de cobranza: wiring + manejo de errores + resumen.

Ejecuta una sesión de cobranza contra un deudor. Cablea las tres capas
(``state`` → ``tools`` → ``agent``), corre el bucle de conversación y, al
cerrar, imprime un resumen y la línea de tiempo del ``historial`` que demuestra
que el perfil se fue actualizando turno a turno.

Uso:  python main.py
"""

from __future__ import annotations

import sys
from typing import Any

# La consola de Windows suele venir en cp1252; forzamos UTF-8 para que los
# acentos y la ñ se vean bien.
for _stream in (sys.stdout, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from agent import AgentError, CobranzaAgent
from config import SNAPSHOT_DIR
from state import EstadoGestion, StateManager
from tools import CobranzaTools

# Obligación bajo gestión en esta sesión (cédula normalizada del deudor semilla).
SESSION_DOCUMENTO = "1082260472"

SALIR = {"salir", "exit", "quit"}


def _trace_tool(name: str, args: dict[str, Any], resultado: Any) -> None:
    """Imprime una traza compacta de cada herramienta que invoca el modelo."""
    es_error = isinstance(resultado, dict) and "error" in resultado
    marca = "✗" if es_error else "✓"
    print(f"   \033[2m↪ [{name}] {marca} {_compacto(resultado)}\033[0m")


def _compacto(valor: Any, limite: int = 160) -> str:
    """Representación corta de un valor para las trazas/timeline."""
    texto = str(valor)
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def imprimir_resumen(state: StateManager) -> None:
    """Imprime el estado final y la línea de tiempo del historial."""
    p = state.profile
    print("\n" + "=" * 64)
    print("RESUMEN DE LA GESTIÓN")
    print("=" * 64)
    print(f"Documento:           {p.documento}")
    print(f"Nombre:              {p.nombre or '(no validado)'}")
    print(f"Identidad validada:  {p.identidad_validada}")
    if p.deuda:
        print(
            f"Deuda:               saldo={p.deuda.saldo} COP | "
            f"mora={p.deuda.dias_mora} días | {p.deuda.producto}"
        )
    print(f"Disposición de pago: {p.disposicion_pago.value}")
    if p.objeciones:
        print("Objeciones:")
        for o in p.objeciones:
            print(f"  - {o.tipo.value}: {o.detalle}")
    if p.compromiso:
        c = p.compromiso
        print(
            f"Compromiso:          {c.monto} COP el {c.fecha} "
            f"(ref. {c.referencia_proceso})"
        )
    print(f"Estado de gestión:   {p.estado_gestion.value}")

    print("\n--- Línea de tiempo (historial, actualización al vuelo) ---")
    if not p.historial:
        print("  (sin cambios registrados)")
    for ev in p.historial:
        print(
            f"  turno {ev.turno} → {ev.campo}: "
            f"{_compacto(ev.valor_anterior, 40)}→{_compacto(ev.valor_nuevo, 40)} "
            f"({ev.origen})"
        )
    print(f"\nPerfil persistido en: {state.snapshot_path}")
    print("=" * 64)


def main() -> None:
    """Punto de entrada: corre una sesión completa de cobranza."""
    state = StateManager(SESSION_DOCUMENTO, SNAPSHOT_DIR)
    tools = CobranzaTools(state)

    print("=" * 64)
    print("  Agente de Cobranza Conversacional — Crecere (demo)")
    print("  Escribe tus respuestas como si fueras el deudor.")
    print("  Comandos: 'salir' / 'exit' para terminar (Ctrl-C también).")
    print("=" * 64 + "\n")

    try:
        agent = CobranzaAgent(tools, SESSION_DOCUMENTO, on_tool_call=_trace_tool)
    except AgentError as exc:
        print(f"[Error de configuración] {exc}")
        return

    turno = 0
    try:
        # El agente (modelo) inicia la llamada: saluda y pide validar identidad.
        turno += 1
        state.turno_actual = turno
        apertura = agent.enviar(
            "[SISTEMA] Inicia tú la llamada: saluda brevemente y solicita validar "
            "la identidad de la persona."
        )
        print(f"Agente: {apertura}\n")

        while True:
            try:
                entrada = input("Tú: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[Sesión interrumpida por el usuario]")
                break

            if not entrada:
                print("(Escribe algo, o 'salir' para terminar.)\n")
                continue
            if entrada.lower() in SALIR:
                print("\n[Cerrando la sesión a petición del usuario]")
                break

            turno += 1
            state.turno_actual = turno
            try:
                respuesta = agent.enviar(entrada)
            except AgentError as exc:
                # Falla del API tras reintentos: avisamos y conservamos el estado.
                print(f"\n[Error del modelo] {exc}")
                print("Se conserva el estado parcial de la gestión.")
                break
            print(f"\nAgente: {respuesta}\n")

    finally:
        # Si la gestión quedó a medias, se marca como ABANDONADA.
        if state.profile.estado_gestion == EstadoGestion.EN_CURSO:
            state.set_estado(EstadoGestion.ABANDONADA, "cierre_sesion")
        imprimir_resumen(state)


if __name__ == "__main__":
    main()
