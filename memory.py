"""
memory.py — Gestión de estado externo del agente.

El estado vive FUERA del LLM. El modelo no tiene memoria implícita;
toda la información relevante se almacena aquí y se pasa explícitamente.
"""

import json
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent.memory")


# ──────────────────────────────────────────────
#  Estructura canónica del estado
# ──────────────────────────────────────────────

DEFAULT_STATE = {
    "objetivo": "",          # Tarea original del usuario
    "plan": [],              # Lista de pasos generados por el planificador
    "paso_actual": 0,        # Índice del paso en ejecución
    "historial": [],         # Registro de eventos (interpretación, ejecución, validación)
    "resultados": [],        # Resultados por cada paso ejecutado
    "intentos_paso": 0,      # Contador de reintentos para el paso actual
    "estado_global": "idle", # idle | running | completed | failed
    "timestamp_inicio": None,
    "timestamp_fin": None,
}


class AgentMemory:
    """
    Repositorio de estado persistente del agente.

    Persiste en memoria RAM y, opcionalmente, en un archivo JSON.
    Toda escritura queda registrada en el historial.
    """

    def __init__(self, persist_path: Optional[str] = None):
        """
        Args:
            persist_path: Ruta al archivo JSON de persistencia.
                          Si es None, el estado solo vive en RAM.
        """
        self._state: dict = {}
        self.persist_path: Optional[Path] = Path(persist_path) if persist_path else None
        self.reset()
        logger.debug("AgentMemory inicializada. Persistencia: %s", self.persist_path or "desactivada")

    # ──────────────────────────────────────────
    #  Inicialización / reset
    # ──────────────────────────────────────────

    def reset(self) -> None:
        """Resetea el estado al estado inicial limpio."""
        import copy
        self._state = copy.deepcopy(DEFAULT_STATE)
        self._state["timestamp_inicio"] = time.time()
        logger.debug("Estado reseteado.")

    # ──────────────────────────────────────────
    #  Getters / Setters atómicos
    # ──────────────────────────────────────────

    def set_objetivo(self, objetivo: str) -> None:
        self._state["objetivo"] = objetivo
        self._save()

    def set_plan(self, plan: list[str]) -> None:
        self._state["plan"] = plan
        self._state["paso_actual"] = 0
        self._state["resultados"] = [""] * len(plan)
        self._state["intentos_paso"] = 0
        self._save()

    def get_plan(self) -> list[str]:
        return self._state["plan"]

    def get_objetivo(self) -> str:
        return self._state["objetivo"]

    def get_paso_actual(self) -> int:
        return self._state["paso_actual"]

    def get_paso_descripcion(self) -> Optional[str]:
        plan = self._state["plan"]
        idx = self._state["paso_actual"]
        if idx < len(plan):
            return plan[idx]
        return None

    def avanzar_paso(self) -> None:
        self._state["paso_actual"] += 1
        self._state["intentos_paso"] = 0
        self._save()

    def incrementar_intento(self) -> int:
        self._state["intentos_paso"] += 1
        self._save()
        return self._state["intentos_paso"]

    def get_intentos(self) -> int:
        return self._state["intentos_paso"]

    def guardar_resultado(self, paso: int, resultado: str) -> None:
        while len(self._state["resultados"]) <= paso:
            self._state["resultados"].append("")
        self._state["resultados"][paso] = resultado
        self._save()

    def get_resultados(self) -> list[str]:
        return self._state["resultados"]

    def set_estado_global(self, estado: str) -> None:
        self._state["estado_global"] = estado
        if estado in ("completed", "failed"):
            self._state["timestamp_fin"] = time.time()
        self._save()

    def get_estado_global(self) -> str:
        return self._state["estado_global"]

    def plan_completo(self) -> bool:
        return self._state["paso_actual"] >= len(self._state["plan"])

    # ──────────────────────────────────────────
    #  Historial de eventos
    # ──────────────────────────────────────────

    def agregar_evento(self, tipo: str, detalle: str) -> None:
        """
        Registra un evento en el historial.

        Args:
            tipo: Etiqueta del evento (ej: 'interpretacion', 'ejecucion', 'validacion').
            detalle: Descripción libre del evento.
        """
        evento = {
            "timestamp": time.time(),
            "tipo": tipo,
            "detalle": detalle,
        }
        self._state["historial"].append(evento)
        logger.debug("[EVENTO] %s: %s", tipo, detalle[:120])
        self._save()

    def get_historial(self) -> list[dict]:
        return self._state["historial"]

    def get_historial_resumido(self, max_eventos: int = 6) -> str:
        """
        Devuelve un resumen compacto de los últimos N eventos.
        Se usa para pasar contexto al LLM sin saturar la ventana de contexto.
        """
        historial = self._state["historial"][-max_eventos:]
        if not historial:
            return "Sin historial previo."
        lineas = []
        for ev in historial:
            ts = time.strftime("%H:%M:%S", time.localtime(ev["timestamp"]))
            lineas.append(f"[{ts}] {ev['tipo'].upper()}: {ev['detalle'][:200]}")
        return "\n".join(lineas)

    # ──────────────────────────────────────────
    #  Contexto comprimido para el LLM
    # ──────────────────────────────────────────

    def contexto_para_paso(self) -> dict:
        """
        Devuelve solo el contexto relevante para el paso actual.
        Evita arrastrar todo el historial al LLM.
        """
        idx = self._state["paso_actual"]
        plan = self._state["plan"]
        pasos_previos = plan[:idx]
        resultados_previos = self._state["resultados"][:idx]

        contexto = {
            "objetivo": self._state["objetivo"],
            "paso_actual": plan[idx] if idx < len(plan) else "N/A",
            "numero_paso": idx + 1,
            "total_pasos": len(plan),
            "pasos_anteriores": pasos_previos,
            "resultados_anteriores": resultados_previos,
            "historial_reciente": self.get_historial_resumido(4),
        }
        return contexto

    # ──────────────────────────────────────────
    #  Snapshot completo (para debug)
    # ──────────────────────────────────────────

    def snapshot(self) -> dict:
        import copy
        return copy.deepcopy(self._state)

    # ──────────────────────────────────────────
    #  Persistencia en disco
    # ──────────────────────────────────────────

    def _save(self) -> None:
        if self.persist_path is None:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("No se pudo guardar el estado en disco: %s", e)

    def load_from_disk(self) -> bool:
        """Carga el estado desde archivo si existe. Retorna True si tuvo éxito."""
        if self.persist_path is None or not self.persist_path.exists():
            return False
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
            logger.info("Estado cargado desde %s", self.persist_path)
            return True
        except Exception as e:
            logger.error("Error cargando estado desde disco: %s", e)
            return False
