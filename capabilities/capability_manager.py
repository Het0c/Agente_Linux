"""
capabilities/capability_manager.py — Router central de capabilities.

Reemplaza el ACTION_REGISTRY del executor original.
Desacopla completamente al executor de la implementación de acciones.

El CapabilityManager:
  - Mantiene un registry de capability → acciones soportadas.
  - Despacha cada acción al handler correcto.
  - Emite eventos de routing al bus (acción desconocida, dispatch, etc.).
  - Soporta registro dinámico de capabilities en runtime.
"""

import logging
from typing import Optional, TYPE_CHECKING

from capabilities.base_capability import BaseCapability

if TYPE_CHECKING:
    from capabilities.event_bus import EventBus

logger = logging.getLogger("agent.capability_manager")


class CapabilityManager:
    """
    Router central: recibe (acción, params) y delega a la capability correcta.

    Uso:
        cm = CapabilityManager(event_bus=bus)
        cm.register(FilesystemCapability(event_bus=bus))
        cm.register(PythonCapability(event_bus=bus))

        exitoso, resultado = cm.dispatch("crear_archivo", {"ruta": "...", "contenido": "..."})
    """

    def __init__(self, event_bus: Optional["EventBus"] = None):
        self.event_bus = event_bus
        # Mapeo: nombre_accion → capability que la maneja
        self._action_map: dict[str, BaseCapability] = {}
        # Lista de capabilities registradas (para introspección)
        self._capabilities: list[BaseCapability] = []
        logger.info("CapabilityManager inicializado.")

    # ──────────────────────────────────────────
    #  Registro de capabilities
    # ──────────────────────────────────────────

    def register(self, capability: BaseCapability) -> None:
        """
        Registra una capability y mapea todas sus acciones soportadas.

        Si una acción ya está registrada, la nueva capability la sobreescribe
        (útil para overrides y testing).
        """
        for action in capability.supported_actions:
            if action in self._action_map:
                previous = self._action_map[action].name
                logger.warning(
                    "Acción '%s' ya registrada por '%s'. "
                    "Sobreescribiendo con '%s'.",
                    action, previous, capability.name,
                )
            self._action_map[action] = capability

        self._capabilities.append(capability)
        logger.info(
            "✔ Capability registrada: '%s' → [%s]",
            capability.name,
            ", ".join(capability.supported_actions),
        )

    def unregister(self, capability_name: str) -> bool:
        """Elimina una capability del registry por nombre."""
        cap = next((c for c in self._capabilities if c.name == capability_name), None)
        if cap is None:
            return False
        self._capabilities = [c for c in self._capabilities if c.name != capability_name]
        self._action_map = {k: v for k, v in self._action_map.items() if v.name != capability_name}
        logger.info("Capability '%s' removida del registry.", capability_name)
        return True

    # ──────────────────────────────────────────
    #  Despacho de acciones
    # ──────────────────────────────────────────

    def dispatch(self, action: str, params: dict) -> tuple[bool, str]:
        """
        Despacha una acción a la capability correcta.

        Args:
            action: Nombre de la acción (ej: "crear_archivo", "ejecutar_python").
            params: Parámetros de la acción (vienen del JSON del modelo code).

        Returns:
            (exitoso: bool, resultado: str)
        """
        if action not in self._action_map:
            available = sorted(self._action_map.keys())
            msg = (
                f"Acción desconocida: '{action}'. "
                f"Disponibles: {available}"
            )
            logger.error(msg)

            # Emitir evento de error al bus para que LearningCapability lo procese
            if self.event_bus:
                self.event_bus.emit(
                    source="CapabilityManager",
                    event_type="error",
                    category="unknown_action",
                    action=action,
                    context={"params_keys": list(params.keys()), "available": available},
                    result=msg,
                    success=False,
                    metadata={"hint": "Revisar EXECUTOR_PROMPT con acciones disponibles"},
                )
            return False, f"ERROR: {msg}"

        capability = self._action_map[action]
        logger.debug("Despachando '%s' → %s", action, capability.name)

        try:
            exitoso, resultado = capability.execute(action, params)
            return exitoso, resultado
        except Exception as e:
            msg = f"Error no capturado en capability '{capability.name}': {e}"
            logger.exception(msg)
            if self.event_bus:
                self.event_bus.emit(
                    source="CapabilityManager",
                    event_type="error",
                    category="capability_crash",
                    action=action,
                    context={"capability": capability.name},
                    result=msg,
                    success=False,
                )
            return False, f"ERROR: {msg}"

    # ──────────────────────────────────────────
    #  Introspección
    # ──────────────────────────────────────────

    def get_available_actions(self) -> list[str]:
        """Lista de todas las acciones disponibles."""
        return sorted(self._action_map.keys())

    def get_capabilities(self) -> list[BaseCapability]:
        return list(self._capabilities)

    def describe(self) -> dict:
        """Dict con el estado completo del registry (útil para debug y prompts)."""
        return {
            cap.name: cap.supported_actions
            for cap in self._capabilities
        }

    def get_actions_for_prompt(self) -> str:
        """
        Genera la descripción de acciones disponibles para insertar en el EXECUTOR_PROMPT.
        Permite que el prompt siempre refleje las capabilities realmente registradas.
        """
        lines = ["Acciones disponibles:\n"]
        for cap in self._capabilities:
            lines.append(f"  [{cap.name}]")
            for action in cap.supported_actions:
                lines.append(f"    - {action}")
        return "\n".join(lines)
