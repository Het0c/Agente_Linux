"""
import json
import logging
from typing import Optional, TYPE_CHECKING

from model_manager import ModelManager
from utils import EXECUTOR_PROMPT, parse_json_response, print_section

if TYPE_CHECKING:
    from capabilities.capability_manager import CapabilityManager
    from capabilities.event_bus import EventBus

logger = logging.getLogger("agent.executor")

"""
class Executor:
    """
    Coordina la generación de acciones (vía LLM) y su despacho (vía CapabilityManager).

    Flujo por paso:
        1. Cargar modelo code
        2. Inferir → obtener acción estructurada JSON
        3. Descargar modelo code
        4. Delegar ejecución al CapabilityManager
        5. Retornar (exitoso, resultado)

    El CapabilityManager se encarga de:
        - Encontrar la capability correcta
        - Ejecutar la acción
        - Emitir eventos al EventBus (que llegan a LearningCapability)
    """

    def __init__(
        self,
        model_manager: ModelManager,
        capability_manager: "CapabilityManager",
        event_bus: Optional["EventBus"] = None,
        debug: bool = False,
    ):
        self.mm = model_manager
        self.capability_manager = capability_manager
        self.event_bus = event_bus
        self.debug = debug

        # Actualizar el EXECUTOR_PROMPT con las acciones realmente disponibles
        self._dynamic_action_list = capability_manager.get_actions_for_prompt()

    # ──────────────────────────────────────────
    #  API pública
    # ──────────────────────────────────────────

    def ejecutar_paso(self, paso: str, contexto: dict) -> tuple[bool, str]:
        """
        Ejecuta un paso del plan.

        Args:
            paso: Descripción del paso a ejecutar.
            contexto: Contexto relevante del AgentMemory para este paso.

        Returns:
            (exitoso: bool, resultado: str)
        """
        print_section("Ejecutor")
        logger.info("Ejecutando paso: %s", paso[:100])

        # 1. Pedir al modelo code que genere la acción estructurada
        accion_dict = self._generar_accion(paso, contexto)
        if accion_dict is None:
            msg = "El modelo code no generó una acción JSON válida."
            # Emitir al bus para que LearningCapability lo registre
            if self.event_bus:
                self.event_bus.emit(
                    source="Executor",
                    event_type="error",
                    category="json_parse_error",
                    action="generar_accion",
                    context={"paso": paso[:100]},
                    result=msg,
                    success=False,
                )
            return False, f"ERROR: {msg}"

        nombre_accion = accion_dict.get("accion", "")
        parametros = accion_dict.get("parametros", {})
        razon = accion_dict.get("razon", "")

        logger.info("Acción generada: '%s' | Razón: %s", nombre_accion, razon[:80])
        if self.debug:
            logger.debug("Parámetros: %s", json.dumps(parametros, ensure_ascii=False)[:300])

        # 2. Delegar al CapabilityManager (que emite sus propios eventos al bus)
        return self.capability_manager.dispatch(nombre_accion, parametros)

    def ejecutar_simple(self, objetivo: str) -> tuple[bool, str]:
        """
        Ejecuta una tarea simple directamente sin plan.

        Args:
            objetivo: Descripción completa de la tarea.

        Returns:
            (exitoso: bool, resultado: str)
        """
        contexto = {
            "objetivo": objetivo,
            "modo": "tarea_simple_sin_plan",
        }
        return self.ejecutar_paso(objetivo, contexto)

    # ──────────────────────────────────────────
    #  Generación de acción via LLM
    # ──────────────────────────────────────────

    def _generar_accion(self, paso: str, contexto: dict) -> Optional[dict]:
        """
        Llama al modelo code para obtener la acción estructurada.

        Carga el modelo → infiere → descarga el modelo.
        El prompt incluye la lista dinámica de acciones del CapabilityManager.
        """
        self.mm.load_model("code")

        # Construir prompt enriquecido con acciones disponibles
        system_prompt = (
            f"{EXECUTOR_PROMPT}\n\n"
            f"ACCIONES REGISTRADAS EN ESTE SISTEMA:\n"
            f"{self._dynamic_action_list}"
        )

        contexto_str = json.dumps(contexto, ensure_ascii=False, indent=2)
        mensaje_usuario = (
            f"CONTEXTO:\n{contexto_str}\n\n"
            f"PASO A EJECUTAR:\n{paso}\n\n"
            f"Genera la acción estructurada JSON para completar este paso."
        )

        try:
            respuesta = self.mm.infer(
                messages=[{"role": "user", "content": mensaje_usuario}],
                system_prompt=system_prompt,
            )
        except RuntimeError as e:
            logger.error("Error en inferencia del modelo code: %s", e)
            return None
        finally:
            self.mm.unload_model()

        accion = parse_json_response(respuesta, context="executor")
        if accion is None:
            logger.warning(
                "Respuesta del modelo code no es JSON válido: %s",
                respuesta[:200],
            )
        return accion