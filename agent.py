"""
agent.py — Agente liviano: intérprete, planificador y validador.

Usa el modelo "agent" (LLM general) para las tres funciones cognitivas
principales. El modelo code (ejecutor) es responsabilidad de executor.py.

IMPORTANTE: El modelo se carga y descarga en cada operación para
conservar RAM. El estado vive en memory.py, no en el modelo.
"""

import json
import logging
from typing import Optional

from model_manager import ModelManager
from memory import AgentMemory
from utils import (
    INTERPRETER_PROMPT,
    PLANNER_PROMPT,
    VALIDATOR_PROMPT,
    parse_json_response,
    print_section,
    print_status,
)

logger = logging.getLogger("agent.core")


class Agent:
    """
    Componente cognitivo del sistema: interpreta, planifica y valida.

    Cada método carga el modelo "agent", realiza una inferencia y
    lo descarga inmediatamente para liberar RAM antes de que el
    ejecutor necesite el modelo "code".
    """

    def __init__(
        self,
        model_manager: ModelManager,
        memory: AgentMemory,
        debug: bool = False,
    ):
        self.mm = model_manager
        self.mem = memory
        self.debug = debug

    # ══════════════════════════════════════════
    #  Intérprete
    # ══════════════════════════════════════════

    def interpretar(self, user_input: str) -> dict:
        """
        Analiza el input del usuario y extrae el objetivo estructurado.

        Args:
            user_input: Texto libre del usuario.

        Returns:
            Dict con claves: objetivo, restricciones, tipo, razon.
            En caso de fallo, devuelve un dict con valores por defecto.
        """
        print_section("Intérprete")
        logger.info("Interpretando input: %s", user_input[:100])

        self.mm.load_model("agent")
        try:
            respuesta = self.mm.infer(
                messages=[{"role": "user", "content": user_input}],
                system_prompt=INTERPRETER_PROMPT,
                max_tokens=256,
                temperature=0.1,
            )
        except RuntimeError as e:
            logger.error("Error en inferencia del intérprete: %s", e)
            return self._fallback_interpretacion(user_input)
        finally:
            self.mm.unload_model()

        resultado = parse_json_response(respuesta, context="interpretar")
        if resultado is None:
            logger.warning("Intérprete no devolvió JSON. Usando fallback.")
            return self._fallback_interpretacion(user_input)

        objetivo = resultado.get("objetivo", user_input)
        tipo = resultado.get("tipo", "compleja")
        razon = resultado.get("razon", "")

        print_status("Objetivo", objetivo)
        print_status("Tipo", tipo)
        if self.debug:
            print_status("Razón", razon)

        self.mem.agregar_evento("interpretacion", f"Objetivo: {objetivo} | Tipo: {tipo}")
        return resultado

    # ══════════════════════════════════════════
    #  Planificador
    # ══════════════════════════════════════════

    def planificar(self, objetivo: str, contexto_adicional: str = "") -> list[str]:
        """
        Descompone el objetivo en una lista de pasos ejecutables.

        Args:
            objetivo: Objetivo claro extraído por el intérprete.
            contexto_adicional: Información extra relevante para el plan.

        Returns:
            Lista de strings, cada uno describiendo un paso.
        """
        print_section("Planificador")
        logger.info("Planificando objetivo: %s", objetivo[:100])

        prompt_usuario = f"OBJETIVO: {objetivo}"
        if contexto_adicional:
            prompt_usuario += f"\n\nCONTEXTO ADICIONAL:\n{contexto_adicional}"

        self.mm.load_model("agent")
        try:
            respuesta = self.mm.infer(
                messages=[{"role": "user", "content": prompt_usuario}],
                system_prompt=PLANNER_PROMPT,
                max_tokens=512,
                temperature=0.2,
            )
        except RuntimeError as e:
            logger.error("Error en inferencia del planificador: %s", e)
            return [objetivo]  # Fallback: tratar como un solo paso
        finally:
            self.mm.unload_model()

        resultado = parse_json_response(respuesta, context="planificar")
        if resultado is None or "pasos" not in resultado:
            logger.warning("Planificador no devolvió JSON válido. Usando objetivo como paso único.")
            return [objetivo]

        pasos = resultado["pasos"]
        razon = resultado.get("razon", "")

        if not isinstance(pasos, list) or len(pasos) == 0:
            logger.warning("Lista de pasos vacía o inválida.")
            return [objetivo]

        # Limitar a máximo 8 pasos (como define el prompt)
        if len(pasos) > 8:
            logger.warning("Plan excede 8 pasos (%d). Truncando.", len(pasos))
            pasos = pasos[:8]

        print_status("Pasos generados", str(len(pasos)))
        for i, paso in enumerate(pasos, 1):
            print(f"    {i}. {paso}")
        if self.debug:
            print_status("Estrategia", razon)

        self.mem.agregar_evento("planificacion", f"{len(pasos)} pasos generados")
        return pasos

    def replanificar(self, desde_paso: int, razon_fallo: str) -> list[str]:
        """
        Regenera el plan desde un punto de fallo.

        Args:
            desde_paso: Índice del paso que falló.
            razon_fallo: Descripción del por qué falló.

        Returns:
            Nueva lista de pasos (reemplaza el plan desde ese punto).
        """
        print_section("Replanificador")
        logger.info("Replanificando desde paso %d. Razón: %s", desde_paso, razon_fallo[:100])

        objetivo = self.mem.get_objetivo()
        plan_actual = self.mem.get_plan()
        pasos_completados = plan_actual[:desde_paso]
        resultados_previos = self.mem.get_resultados()[:desde_paso]

        contexto = {
            "objetivo_original": objetivo,
            "pasos_completados": pasos_completados,
            "resultados_previos": resultados_previos,
            "fallo_en_paso": plan_actual[desde_paso] if desde_paso < len(plan_actual) else "N/A",
            "razon_fallo": razon_fallo,
        }
        contexto_str = json.dumps(contexto, ensure_ascii=False, indent=2)

        prompt_usuario = (
            f"El plan original falló en el paso {desde_paso + 1}.\n\n"
            f"CONTEXTO DEL FALLO:\n{contexto_str}\n\n"
            f"Genera un nuevo plan (solo los pasos restantes) para completar el objetivo."
        )

        self.mm.load_model("agent")
        try:
            respuesta = self.mm.infer(
                messages=[{"role": "user", "content": prompt_usuario}],
                system_prompt=PLANNER_PROMPT,
                max_tokens=512,
            )
        except RuntimeError as e:
            logger.error("Error en replanificación: %s", e)
            return []
        finally:
            self.mm.unload_model()

        resultado = parse_json_response(respuesta, context="replanificar")
        if resultado is None or "pasos" not in resultado:
            return []

        nuevos_pasos = resultado["pasos"]
        logger.info("Nuevos pasos generados: %d", len(nuevos_pasos))
        self.mem.agregar_evento("replanificacion", f"Replanificado desde paso {desde_paso}. Nuevos pasos: {len(nuevos_pasos)}")
        return nuevos_pasos

    # ══════════════════════════════════════════
    #  Validador
    # ══════════════════════════════════════════

    def validar(
        self,
        paso: str,
        resultado_ejecucion: str,
        exitoso_ejecucion: bool,
        sugerencia_previa: str = "",
    ) -> dict:
        """
        Evalúa si el resultado de un paso es correcto.

        Args:
            paso: Descripción del paso ejecutado.
            resultado_ejecucion: Output del ejecutor.
            exitoso_ejecucion: Si el ejecutor reportó éxito.
            sugerencia_previa: Sugerencia del validador en el intento anterior.

        Returns:
            Dict con claves: decision, exitoso, razon, sugerencia.
            decision ∈ {"continuar", "repetir", "replanificar"}
        """
        print_section("Validador")
        logger.info("Validando resultado del paso: %s", paso[:80])

        # Si el ejecutor ya reportó un error grave, se puede decidir sin LLM
        if resultado_ejecucion.startswith("ERROR:") and not exitoso_ejecucion:
            if "path traversal" in resultado_ejecucion.lower() or "permiso" in resultado_ejecucion.lower():
                decision = {
                    "decision": "replanificar",
                    "exitoso": False,
                    "razon": "Error de seguridad irrecuperable.",
                    "sugerencia": resultado_ejecucion,
                }
                self.mem.agregar_evento("validacion", f"REPLANIFICAR (error seguridad): {resultado_ejecucion[:100]}")
                return decision

        contexto_validacion = {
            "objetivo_general": self.mem.get_objetivo(),
            "paso_evaluado": paso,
            "resultado_obtenido": resultado_ejecucion[:1000],  # Limitar para el contexto
            "ejecucion_exitosa": exitoso_ejecucion,
            "sugerencia_anterior": sugerencia_previa,
        }
        prompt_usuario = (
            f"Evalúa el siguiente resultado de ejecución:\n\n"
            f"{json.dumps(contexto_validacion, ensure_ascii=False, indent=2)}"
        )

        self.mm.load_model("agent")
        try:
            respuesta = self.mm.infer(
                messages=[{"role": "user", "content": prompt_usuario}],
                system_prompt=VALIDATOR_PROMPT,
                max_tokens=256,
                temperature=0.1,
            )
        except RuntimeError as e:
            logger.error("Error en inferencia del validador: %s", e)
            # Fallback conservador: si el ejecutor dijo OK, continuar
            return {
                "decision": "continuar" if exitoso_ejecucion else "repetir",
                "exitoso": exitoso_ejecucion,
                "razon": f"Validador no disponible: {e}",
                "sugerencia": "",
            }
        finally:
            self.mm.unload_model()

        resultado = parse_json_response(respuesta, context="validar")
        if resultado is None:
            logger.warning("Validador no devolvió JSON. Usando heurística simple.")
            return {
                "decision": "continuar" if exitoso_ejecucion else "repetir",
                "exitoso": exitoso_ejecucion,
                "razon": "Validación heurística (fallo de parseo).",
                "sugerencia": "",
            }

        decision = resultado.get("decision", "repetir")
        exitoso = resultado.get("exitoso", False)
        razon = resultado.get("razon", "")
        sugerencia = resultado.get("sugerencia", "")

        icono = {"continuar": "✅", "repetir": "⚠️", "replanificar": "🔄"}.get(decision, "❓")
        print_status("Decisión", f"{icono} {decision.upper()}")
        if self.debug or decision != "continuar":
            print_status("Razón", razon)
            if sugerencia:
                print_status("Sugerencia", sugerencia)

        self.mem.agregar_evento(
            "validacion",
            f"Decision={decision} | Paso={paso[:60]} | Razón={razon[:80]}"
        )

        return {"decision": decision, "exitoso": exitoso, "razon": razon, "sugerencia": sugerencia}

    # ──────────────────────────────────────────
    #  Helpers internos
    # ──────────────────────────────────────────

    def _fallback_interpretacion(self, user_input: str) -> dict:
        """Interpretación mínima cuando el LLM falla."""
        return {
            "objetivo": user_input,
            "restricciones": [],
            "tipo": "compleja",
            "razon": "Fallback: intérprete no disponible",
        }
