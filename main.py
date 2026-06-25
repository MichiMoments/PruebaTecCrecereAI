"""Consola (REPL) del agente de cobranza: wiring + manejo de errores + resumen.

Ejecuta una sesión de cobranza contra un deudor. Cablea las tres capas
(``state`` → ``tools`` → ``agent``), corre el bucle de conversación y, al
cerrar, imprime un resumen y la línea de tiempo del ``historial`` que demuestra
que el perfil se fue actualizando turno a turno.

Uso:  python main.py [cedula]
      Sin argumento gestiona el deudor por defecto; con una cédula sembrada
      (con o sin puntos) gestiona ese deudor.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

# La consola de Windows suele venir en cp1252; forzamos UTF-8 para que los
# acentos y la ñ se vean bien.
for _stream in (sys.stdout, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from agent import AgentError, CobranzaAgent
from config import SNAPSHOT_DIR
from data import buscar_deudor, listar_deudores, normalizar_documento, primer_nombre
from state import EstadoGestion, StateManager, TipoContacto
from tools import CobranzaTools

# Cédula del deudor por defecto (si no se pasa una por argumento de línea de
# comandos). Se puede gestionar cualquier otra cédula sembrada con
# `python main.py <cedula>`.
DEFAULT_DOCUMENTO = "1082260472"

SALIR = {"salir", "exit", "quit"}


def _resolver_documento(argv: list[str]) -> Optional[str]:
    """Resuelve qué cédula gestiona la sesión a partir de los argumentos.

    - Sin argumento → ``DEFAULT_DOCUMENTO``.
    - Con una cédula sembrada (con o sin puntos) → esa cédula normalizada.
    - Con una cédula inexistente → imprime el error y las cédulas disponibles y
      devuelve ``None`` para que el programa termine.
    """
    if len(argv) <= 1:
        return DEFAULT_DOCUMENTO

    documento = normalizar_documento(argv[1])
    if buscar_deudor(documento) is not None:
        return documento

    print(f"[Error] No hay ningún deudor con la cédula '{argv[1]}'.")
    print("Deudores disponibles:")
    for rec in listar_deudores():
        print(
            f"  - {rec['documento']}  {rec['nombre']} ({rec['producto']})"
        )
    print("\nUso: python main.py [cedula]")
    return None


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
    if p.tipo_contacto != TipoContacto.DESCONOCIDO:
        print(f"Tipo de contacto:    {p.tipo_contacto.value}")
    if p.nota_contacto:
        print(f"Recado:              {p.nota_contacto}")
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
    documento = _resolver_documento(sys.argv)
    if documento is None:
        return

    state = StateManager(documento, SNAPSHOT_DIR)
    tools = CobranzaTools(state)

    # Banner del lado del gestor: a quién corresponde la obligación gestionada
    # (info de la consola, no se le revela al titular sin validar).
    deudor = buscar_deudor(documento)
    print("=" * 64)
    print("  Agente de Cobranza Conversacional — Crecere (demo)")
    print(
        f"  Sesión sobre: {documento} · {deudor['nombre']} · {deudor['producto']}"
    )
    print("  Escribe tus respuestas como si fueras el deudor.")
    print("  Comandos: 'salir' / 'exit' para terminar (Ctrl-C también).")
    print("=" * 64 + "\n")

    try:
        agent = CobranzaAgent(
            tools,
            documento,
            primer_nombre(deudor["nombre"]),
            on_tool_call=_trace_tool,
        )
    except AgentError as exc:
        print(f"[Error de configuración] {exc}")
        return

    turno = 0
    try:
        # El agente (modelo) inicia la llamada: saluda y pide validar identidad.
        turno += 1
        state.turno_actual = turno
        apertura = agent.enviar(
            "[SISTEMA] Inicia tú la llamada: saluda y pregunta si hablas con la "
            "persona usando SOLO su primer nombre; cuando confirme, solicita validar "
            "su identidad (nombre completo y fecha de nacimiento)."
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
