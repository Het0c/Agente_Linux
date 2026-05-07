"""
main.py — Punto de entrada y loop principal del agente.

Implementa el flujo de control FUERA del LLM:
  1. Interpretar input
  2. Decidir: simple → ejecutar directo | compleja → planificar
  3. Loop de ejecución por pasos:
     a. Ejecutar paso con modelo code
     b. Validar resultado con modelo agent
     c. Decidir: continuar | repetir | replanificar
  4. Finalizar con resumen

El LLM nunca controla el flujo. Python controla el flujo.
"""

import sys
import logging
import argparse
from typing import Optional

from model_manager import ModelManager
from telemetry import TelemetryBus, AgentEvent
from memory import AgentMemory
from agent import Agent
from executor import Executor
from utils import (
    is_simple_task,
    print_banner,
    print_step,
    print_status,
    print_result,
    print_section,
)


# ──────────────────────────────────────────────
#  Constantes de control del loop
# ──────────────────────────────────────────────

MAX_INTENTOS_POR_PASO = 3      # Reintentos máximos antes de replanificar
MAX_REPLANIFICACIONES = 2      # Máximo de veces que se puede replanificar


# ──────────────────────────────────────────────
#  Configuración de logging
# ──────────────────────────────────────────────

def setup_logging(debug: bool = False) -> None:
    nivel = logging.DEBUG if debug else logging.INFO
    formato = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=nivel,
        format=formato,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("agent.log", encoding="utf-8"),
        ],
    )
    # Silenciar logs muy verbosos de librerías externas
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


logger = logging.getLogger("agent.main")


# ══════════════════════════════════════════════
#  Orquestador principal
# ══════════════════════════════════════════════

class AgentOrchestrator:
    """
    Coordina el flujo completo de ejecución del agente.

    Responsabilidades:
    - Gestionar el loop de ejecución.
    - Decidir cuándo usar bypass simple vs. plan completo.
    - Manejar reintentos, replanificaciones y fallbacks.
    - Mantener el estado en AgentMemory.
    """

    def __init__(
        self,
        persist_path: Optional[str] = None,
        debug: bool = False,
    ):
        self.debug = debug

        # Inicializar componentes
        self.memory = AgentMemory(persist_path=persist_path)
        self.telemetry = TelemetryBus()
        self.task_id = "task-main"
        self.model_manager = ModelManager(debug=debug, telemetry=self.telemetry, task_id=self.task_id)
        self.agent = Agent(self.model_manager, self.memory, debug=debug)
        self.executor = Executor(self.model_manager, debug=debug)

        self._replanificaciones = 0

        logger.info("AgentOrchestrator inicializado. Debug=%s", debug)

    # ──────────────────────────────────────────
    #  Punto de entrada principal
    # ──────────────────────────────────────────

    def run(self, user_input: str) -> str:
        """
        Ejecuta el agente completo para un input del usuario.

        Args:
            user_input: Tarea o pregunta del usuario.

        Returns:
            Resultado final como string.
        """
        print_banner(f"AGENTE LLM  |  Tarea: {user_input[:50]}...")
        logger.info("=== Nueva tarea recibida ===")
        self.telemetry.emit_event(AgentEvent(task_id=self.task_id, event_type="task_received", severity="info", payload={"input": user_input[:200]}))
        logger.info("Input: %s", user_input)

        # Resetear estado
        self.memory.reset()
        self.memory.set_objetivo(user_input)
        self.memory.set_estado_global("running")
        self._replanificaciones = 0

        try:
            resultado = self._run_interno(user_input)
            self.memory.set_estado_global("completed")
            self.telemetry.emit_event(AgentEvent(task_id=self.task_id, event_type="task_completed", severity="info", payload={"result": resultado[:300]}))
            return resultado
        except KeyboardInterrupt:
            print("\n\n  ⚠️  Ejecución interrumpida por el usuario.")
            self.memory.set_estado_global("failed")
            self.memory.agregar_evento("interrupcion", "Cancelado por el usuario.")
            return "Tarea cancelada."
        except Exception as e:
            logger.exception("Error fatal en el agente.")
            self.memory.set_estado_global("failed")
            self.memory.agregar_evento("error_fatal", str(e))
            self.telemetry.emit_event(AgentEvent(task_id=self.task_id, event_type="task_failed", severity="error", payload={"error": str(e)}))
            return f"Error fatal: {e}"

    def _run_interno(self, user_input: str) -> str:
        """Lógica interna del loop principal."""

        # ── PASO 1: Interpretación ────────────────
        interpretacion = self.agent.interpretar(user_input)
        objetivo = interpretacion.get("objetivo", user_input)
        tipo = interpretacion.get("tipo", "compleja")
        self.memory.set_objetivo(objetivo)

        # ── PASO 2: ¿Tarea simple? (bypass) ──────
        # Doble check: heurística local + clasificación del LLM
        es_simple = is_simple_task(user_input) or tipo == "simple"

        if es_simple:
            return self._ejecutar_simple(objetivo)
        else:
            return self._ejecutar_compleja(objetivo, interpretacion)

    # ──────────────────────────────────────────
    #  Flujo simple (bypass del planificador)
    # ──────────────────────────────────────────

    def _ejecutar_simple(self, objetivo: str) -> str:
        """Ejecuta una tarea simple directamente, sin plan."""
        print_section("Modo Simple (bypass de planificador)")
        logger.info("Tarea simple detectada. Ejecución directa.")

        exitoso, resultado = self.executor.ejecutar_simple(objetivo)
        print_result(resultado, exitoso)

        if not exitoso:
            logger.warning("Tarea simple falló. Resultado: %s", resultado)
            self.memory.agregar_evento("simple_fallo", resultado[:200])
            self.telemetry.emit_event(AgentEvent(task_id=self.task_id, event_type="simple_failed", severity="warning", payload={"result": resultado[:200]}))
        else:
            self.memory.agregar_evento("simple_ok", resultado[:200])
            self.telemetry.emit_event(AgentEvent(task_id=self.task_id, event_type="simple_ok", severity="info", payload={"result": resultado[:200]}))

        return resultado

    # ──────────────────────────────────────────
    #  Flujo complejo (con planificación y loop)
    # ──────────────────────────────────────────

    def _ejecutar_compleja(self, objetivo: str, interpretacion: dict) -> str:
        """Ejecuta una tarea compleja con plan y loop de validación."""

        restricciones = interpretacion.get("restricciones", [])
        contexto_extra = ""
        if restricciones:
            contexto_extra = "Restricciones: " + "; ".join(restricciones)

        # ── PASO 3: Planificación ─────────────────
        plan = self.agent.planificar(objetivo, contexto_extra)
        if not plan:
            logger.error("El planificador devolvió un plan vacío.")
            return "Error: No se pudo generar un plan para la tarea."

        self.memory.set_plan(plan)

        # ── PASO 4: Loop de ejecución ─────────────
        return self._loop_ejecucion()

    def _loop_ejecucion(self) -> str:
        """
        Loop principal de ejecución: ejecutar → validar → decidir.

        Este es el corazón del sistema. El flujo lo controla Python,
        no el LLM.

        Returns:
            Resultado final de la ejecución.
        """
        print_banner("Loop de Ejecución", char="─")
        sugerencia_validador = ""

        while not self.memory.plan_completo():
            idx = self.memory.get_paso_actual()
            plan = self.memory.get_plan()
            paso = plan[idx]
            total = len(plan)

            print_step(idx + 1, total, paso)

            # ── Ejecutar paso ──────────────────────
            contexto = self.memory.contexto_para_paso()
            exitoso_exec, resultado_exec = self.executor.ejecutar_paso(paso, contexto)
            self.memory.guardar_resultado(idx, resultado_exec)
            print_result(resultado_exec, exitoso_exec)

            # ── Validar resultado ──────────────────
            validacion = self.agent.validar(
                paso,
                resultado_exec,
                exitoso_exec,
                sugerencia_previa=sugerencia_validador,
            )

            decision = validacion["decision"]
            sugerencia_validador = validacion.get("sugerencia", "")

            # ── Decidir próximo paso ───────────────
            if decision == "continuar":
                logger.info("Paso %d/%d completado. ✅", idx + 1, total)
                self.memory.avanzar_paso()
                sugerencia_validador = ""  # Reset sugerencia al avanzar

            elif decision == "repetir":
                intentos = self.memory.incrementar_intento()
                logger.warning("Paso %d fallido. Intento %d/%d.", idx + 1, intentos, MAX_INTENTOS_POR_PASO)
                print_status("Reintentando", f"intento {intentos}/{MAX_INTENTOS_POR_PASO}", "⚠️")

                if intentos >= MAX_INTENTOS_POR_PASO:
                    logger.warning("Máximo de intentos alcanzado. Replanificando.")
                    resultado = self._intentar_replanificacion(idx, validacion["razon"])
                    if resultado is not None:
                        return resultado
                    # Si replanificación exitosa, continuar el loop con el nuevo plan

            elif decision == "replanificar":
                logger.warning("Validador solicita replanificación desde paso %d.", idx + 1)
                resultado = self._intentar_replanificacion(idx, validacion["razon"])
                if resultado is not None:
                    return resultado
                # Si replanificación exitosa, continuar el loop

            else:
                logger.error("Decisión desconocida: '%s'. Continuando.", decision)
                self.memory.avanzar_paso()

        # ── Plan completado ────────────────────────
        return self._generar_resumen_final()

    def _intentar_replanificacion(self, desde_paso: int, razon: str) -> Optional[str]:
        """
        Intenta replanificar desde un punto de fallo.

        Returns:
            None si la replanificación fue exitosa (continuar el loop),
            o un mensaje de error si se agotaron los intentos.
        """
        self._replanificaciones += 1

        if self._replanificaciones > MAX_REPLANIFICACIONES:
            msg = (
                f"Máximo de replanificaciones alcanzado ({MAX_REPLANIFICACIONES}). "
                "No se pudo completar la tarea."
            )
            logger.error(msg)
            print_status("ERROR FATAL", msg, "❌")
            self.memory.agregar_evento("fallo_final", msg)
            return msg

        print_status(
            "Replanificación",
            f"intento {self._replanificaciones}/{MAX_REPLANIFICACIONES}",
            "🔄"
        )

        nuevos_pasos = self.agent.replanificar(desde_paso, razon)
        if not nuevos_pasos:
            msg = "Replanificación fallida: el planificador no generó nuevos pasos."
            logger.error(msg)
            return msg

        # Actualizar el plan en memoria: mantener pasos completados + nuevos pasos
        plan_actual = self.memory.get_plan()
        plan_completado = plan_actual[:desde_paso]
        nuevo_plan_completo = plan_completado + nuevos_pasos

        self.memory.set_plan(nuevo_plan_completo)
        # Volver al punto de fallo (el índice no se avanza automáticamente)
        self.memory._state["paso_actual"] = desde_paso
        self.memory._state["intentos_paso"] = 0
        self.memory._save()

        logger.info(
            "Plan reemplazado. Nuevo total: %d pasos. Continuando desde paso %d.",
            len(nuevo_plan_completo),
            desde_paso + 1,
        )

        print_banner(f"Nuevo Plan ({len(nuevos_pasos)} pasos restantes)", char="·")
        for i, p in enumerate(nuevos_pasos, desde_paso + 1):
            print(f"    {i}. {p}")

        return None  # Continuar el loop

    def _generar_resumen_final(self) -> str:
        """Genera y muestra el resumen de la ejecución completada."""
        print_banner("✅ TAREA COMPLETADA", char="═")

        resultados = self.memory.get_resultados()
        plan = self.memory.get_plan()

        lineas = [f"Objetivo: {self.memory.get_objetivo()}\n"]
        lineas.append("Resumen de pasos:\n")

        for i, (paso, resultado) in enumerate(zip(plan, resultados), 1):
            lineas.append(f"  {i}. {paso}")
            if resultado:
                preview = resultado[:100] + ("..." if len(resultado) > 100 else "")
                lineas.append(f"     → {preview}")

        resumen = "\n".join(lineas)
        print(resumen)

        logger.info("Tarea completada. Pasos: %d", len(plan))
        self.memory.agregar_evento("completado", f"Plan de {len(plan)} pasos completado.")

        # Último resultado como valor de retorno principal
        return resultados[-1] if resultados else "Tarea completada."


# ══════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agente LLM híbrido con arquitectura desacoplada.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python main.py "Crea un archivo con los 10 primeros números de Fibonacci"
  python main.py --debug "Analiza el código en src/ y genera un informe"
  python main.py --persist estado.json "Continua la tarea anterior"
  python main.py --interactive
        """,
    )
    parser.add_argument(
        "tarea",
        nargs="?",
        help="Tarea a ejecutar (si no se provee, entra en modo interactivo).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Activa el modo debug con logging detallado.",
    )
    parser.add_argument(
        "--persist",
        metavar="ARCHIVO",
        help="Ruta del archivo JSON para persistir el estado del agente.",
        default=None,
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Modo interactivo: el agente espera tareas en un loop.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(debug=args.debug)

    orchestrator = AgentOrchestrator(
        persist_path=args.persist,
        debug=args.debug,
    )

    if args.interactive or args.tarea is None:
        # Modo interactivo
        print_banner("AGENTE LLM — Modo Interactivo")
        print("  Escribe tu tarea y presiona Enter. 'salir' para terminar.\n")

        while True:
            try:
                user_input = input("  ▶ Tarea: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Hasta luego.")
                break

            if not user_input:
                continue
            if user_input.lower() in ("salir", "exit", "quit", "q"):
                print("  Hasta luego.")
                break

            resultado = orchestrator.run(user_input)
            print(f"\n  📋 Resultado final:\n  {resultado}\n")
    else:
        # Modo único: ejecutar la tarea pasada por argumento
        resultado = orchestrator.run(args.tarea)
        print(f"\n  📋 Resultado final:\n  {resultado}")


if __name__ == "__main__":
    main()
