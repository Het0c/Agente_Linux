"""
capabilities/event_bus.py — Bus de eventos para desacoplar emisores de observadores.

Cualquier componente puede emitir eventos sin saber quién los escucha.
LearningCapability se suscribe aquí. En el futuro, otros observadores también pueden.

El bus es síncronico y silencioso: si un observer falla, no bloquea al emisor.
"""

import uuid
import time
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("agent.event_bus")


@runtime_checkable
class EventObserver(Protocol):
    """Interfaz que debe implementar cualquier observador del bus."""
    def on_event(self, event: dict) -> None: ...


class EventBus:
    """
    Bus de eventos pub/sub sincrónico.

    Emisores:  capability_manager, filesystem, python, agent, executor...
    Observadores: LearningCapability (y cualquier otro que se suscriba)

    El bus NO persiste eventos. Solo los despacha en tiempo real.
    """

    def __init__(self):
        self._observers: list[EventObserver] = []
        logger.debug("EventBus inicializado.")

    def subscribe(self, observer: EventObserver) -> None:
        """Suscribe un observador. Acepta cualquier objeto con on_event()."""
        self._observers.append(observer)
        logger.debug("Observer suscrito: %s", type(observer).__name__)

    def unsubscribe(self, observer: EventObserver) -> None:
        self._observers = [o for o in self._observers if o is not observer]

    def emit(
        self,
        source: str,
        event_type: str,
        category: str,
        action: str = "",
        context: dict = None,
        result: str = "",
        success: bool = True,
        metadata: dict = None,
    ) -> None:
        """
        Emite un evento a todos los observers suscritos.

        Args:
            source:     Nombre del componente emisor (ej: "FilesystemCapability").
            event_type: Tipo semántico: "error" | "success" | "insight" | "pattern" | "feature_request".
            category:   Categoría específica: "file_not_found" | "syntax_error" | ...
            action:     Acción que originó el evento (ej: "leer_archivo").
            context:    Dict con parámetros relevantes para el diagnóstico.
            result:     Resultado crudo (truncado para no saturar el bus).
            success:    Si la operación fue exitosa.
            metadata:   Datos opcionales específicos del evento.
        """
        event = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": time.time(),
            "source": source,
            "event_type": event_type,
            "category": category,
            "action": action,
            "context": context or {},
            "result": result[:500] if result else "",
            "success": success,
            "metadata": metadata or {},
        }

        for observer in self._observers:
            try:
                observer.on_event(event)
            except Exception as e:
                # Los observers nunca deben romper el flujo principal
                logger.debug(
                    "Observer '%s' lanzó excepción silenciada: %s",
                    type(observer).__name__,
                    e,
                )
