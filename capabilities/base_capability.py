"""
capabilities/base_capability.py — Clase base abstracta para todas las capabilities.

Cada capability encapsula:
  - Lógica de ejecución (dominio propio)
  - Memoria individual (workspace/capabilities/<nombre>/memory.jsonl)
  - Emisión de eventos al EventBus

El patrón de Capability Lifecycle:
    Observa → Aprende → Reflexiona → Propone → Valida → Actualiza
"""

import json
import time
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from capabilities.event_bus import EventBus

logger = logging.getLogger("agent.capability")


class BaseCapability(ABC):
    """
    Base para todas las capabilities del agente.

    Una capability NO es un ejecutor genérico: tiene identidad propia,
    aprende de sus propios errores y los reporta de forma estructurada.
    """

    def __init__(self, event_bus: Optional["EventBus"] = None):
        self.event_bus = event_bus
        self.name = self.__class__.__name__
        self._setup_memory()
        logger.debug("Capability '%s' inicializada.", self.name)

    # ──────────────────────────────────────────
    #  Contrato obligatorio (abstracto)
    # ──────────────────────────────────────────

    @property
    @abstractmethod
    def supported_actions(self) -> list[str]:
        """Lista de nombres de acciones que esta capability puede ejecutar."""
        ...

    @abstractmethod
    def execute(self, action: str, params: dict) -> tuple[bool, str]:
        """
        Ejecuta una acción y devuelve (exitoso, resultado_str).

        La capability es responsable de:
        - Ejecutar la acción de forma segura.
        - Emitir eventos al bus (éxito o error).
        - Escribir en su memoria propia si es relevante.
        """
        ...

    # ──────────────────────────────────────────
    #  Emisión de eventos (no bloquea si no hay bus)
    # ──────────────────────────────────────────

    def emit(
        self,
        event_type: str,
        category: str,
        action: str = "",
        context: dict = None,
        result: str = "",
        success: bool = True,
        metadata: dict = None,
    ) -> None:
        """Emite un evento al bus si hay uno conectado."""
        if self.event_bus:
            self.event_bus.emit(
                source=self.name,
                event_type=event_type,
                category=category,
                action=action,
                context=context,
                result=result,
                success=success,
                metadata=metadata,
            )

    # ──────────────────────────────────────────
    #  Memoria propia de la capability
    # ──────────────────────────────────────────

    def _setup_memory(self) -> None:
        """
        Crea el directorio y archivo JSONL de memoria propio.

        Cada capability tiene su propio espacio:
            workspace/capabilities/filesystem/memory.jsonl
            workspace/capabilities/python/memory.jsonl
            ...

        Esto evita mezclar conocimiento de dominios distintos.
        """
        cap_name = self.name.lower().replace("capability", "").strip()
        self.memory_path = Path(f"workspace/capabilities/{cap_name}/memory.jsonl")
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_memory(self, data: dict) -> None:
        """
        Persiste un registro en la memoria de esta capability.

        El formato JSONL permite:
            grep, jq, embeddings, sqlite, vector db
        sin parsear Markdown ni JSON anidado.
        """
        record = {"timestamp": time.time(), **data}
        try:
            with open(self.memory_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("No se pudo escribir en memoria de '%s': %s", self.name, e)

    def read_memory(self, last_n: int = 50) -> list[dict]:
        """
        Lee los últimos N registros de la memoria de esta capability.

        Útil para que la capability "recuerde" qué ha hecho recientemente
        y ajuste su comportamiento (ej: evitar rutas que fallaron antes).
        """
        if not self.memory_path.exists():
            return []
        try:
            text = self.memory_path.read_text(encoding="utf-8").strip()
            lines = [l for l in text.split("\n") if l.strip()]
            records = []
            for line in lines[-last_n:]:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            return records
        except Exception:
            return []

    def get_error_history(self, action: str = None, last_n: int = 20) -> list[dict]:
        """Filtra la memoria para errores, opcionalmente por acción."""
        records = self.read_memory(last_n * 3)
        return [
            r for r in records
            if not r.get("success", True)
            and (action is None or r.get("action") == action)
        ][-last_n:]
