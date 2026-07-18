"""
capabilities/general_capability.py — Acciones generales y de utilidad.

Maneja acciones que no pertenecen a un dominio específico:
  - responder: devolver texto directo al usuario
  - listar_acciones: introspección del CapabilityManager

No tiene memoria propia relevante (sus operaciones no fallan de formas
aprendibles). Emite eventos mínimos.
"""

import logging
from typing import Optional, TYPE_CHECKING

from capabilities.base_capability import BaseCapability

if TYPE_CHECKING:
    from capabilities.capability_manager import CapabilityManager
    from capabilities.event_bus import EventBus

logger = logging.getLogger("agent.general_cap")


class GeneralCapability(BaseCapability):
    """
    Capability para acciones de propósito general.

    Mantiene una referencia al CapabilityManager para poder responder
    a listar_acciones con la lista real de capabilities registradas.
    """

    def __init__(
        self,
        capability_manager: Optional["CapabilityManager"] = None,
        event_bus: Optional["EventBus"] = None,
    ):
        super().__init__(event_bus=event_bus)
        self.capability_manager = capability_manager

    @property
    def supported_actions(self) -> list[str]:
        return ["responder", "listar_acciones"]

    def execute(self, action: str, params: dict) -> tuple[bool, str]:
        if action == "responder":
            return self._responder(params)
        elif action == "listar_acciones":
            return self._listar_acciones(params)
        return False, f"ERROR: Acción no soportada por GeneralCapability: '{action}'"

    def _responder(self, params: dict) -> tuple[bool, str]:
        """Devuelve una respuesta directa al usuario."""
        mensaje = params.get("mensaje", "")
        if not mensaje:
            return False, "ERROR: El parámetro 'mensaje' está vacío."
        self.emit("success", "direct_response", action="responder",
                  result=mensaje[:100], success=True)
        return True, mensaje

    def _listar_acciones(self, _params: dict) -> tuple[bool, str]:
        """Lista todas las acciones disponibles en el sistema."""
        if self.capability_manager:
            acciones = self.capability_manager.get_available_actions()
            resultado = "Acciones disponibles:\n" + "\n".join(f"  - {a}" for a in acciones)
        else:
            resultado = "CapabilityManager no disponible para introspección."
        return True, resultado
