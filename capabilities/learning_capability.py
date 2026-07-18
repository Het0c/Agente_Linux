"""
capabilities/learning_capability.py — Motor de aprendizaje continuo.

⚠️  ESTA CAPABILITY NUNCA PARTICIPA EN EL FLUJO DE EJECUCIÓN.
    Solo observa. Solo clasifica. Solo guarda.

Flujo:
    Cualquier componente
        ↓
    EventBus.emit(...)
        ↓
    LearningCapability.on_event(event)
        ↓
    Clasificar → Guardar en JSONL correcto
        ↓
    Si patrón detectado → Escribir a pending/

Estructura en disco:
    workspace/
        memory/
            learnings/
                errors.jsonl          ← errores capturados
                insights.jsonl        ← observaciones positivas relevantes
                patterns.jsonl        ← patrones detectados (mismo error N veces)
                improvements.jsonl    ← propuestas de mejora generadas
                feature_requests.jsonl← funcionalidades pendientes detectadas
            pending/
                learning-{id}.json    ← patrones en cola de revisión/promoción
            verified/
                learning-{id}.json    ← aprobados manualmente o por ReflectionCapability
            promoted/
                learning-{id}.json    ← integrados al sistema

¿Por qué JSONL?
    - Una línea = un evento = un registro
    - Compatible con grep, jq, pandas, sqlite, embeddings, vector db
    - Append-only: no hay corrupción por escritura concurrente
    - Legible por humanos sin herramientas especiales
"""

import json
import uuid
import time
import logging
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger("agent.learning")

# Número de errores del mismo tipo antes de crear un patrón y enviarlo a pending/
PATTERN_THRESHOLD = 3

# Workspace base para toda la memoria del sistema
MEMORY_ROOT = Path("workspace/memory")


class LearningCapability:
    """
    Observador pasivo del sistema de agente.

    No ejecuta. No planifica. No valida.
    Solo escucha, clasifica y persiste.

    La LearningCapability es el "sistema nervioso" del agente:
    registra todo lo que pasa para que otros componentes (ReflectionCapability,
    SkillBuilder) puedan aprender de ello.
    """

    def __init__(self):
        self._setup_directories()
        # Contadores en RAM para detección rápida de patrones
        # key: "source:category" → count
        self._error_counts: dict[str, int] = defaultdict(int)
        # Patrones ya creados (para no duplicar)
        self._created_patterns: set[str] = set()
        # Stats globales
        self._total_events = 0
        self._events_by_type: dict[str, int] = defaultdict(int)
        logger.info("LearningCapability inicializada. Workspace: %s", MEMORY_ROOT)

    # ──────────────────────────────────────────
    #  Directorios
    # ──────────────────────────────────────────

    def _setup_directories(self) -> None:
        dirs = [
            MEMORY_ROOT / "learnings",
            MEMORY_ROOT / "pending",
            MEMORY_ROOT / "verified",
            MEMORY_ROOT / "promoted",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────
    #  Punto de entrada (llamado por EventBus)
    # ──────────────────────────────────────────

    def on_event(self, event: dict) -> None:
        """
        Recibe y procesa un evento del bus.

        NUNCA debe lanzar excepciones: errores aquí no deben romper el flujo.
        Todo se silencia con un log de debug.
        """
        try:
            self._total_events += 1
            event_type = event.get("event_type", "unknown")
            self._events_by_type[event_type] += 1
            self._route(event)
        except Exception as e:
            logger.debug("LearningCapability: error silenciado procesando evento: %s", e)

    def _route(self, event: dict) -> None:
        """Clasifica el evento y lo dirige al handler correcto."""
        event_type = event.get("event_type", "unknown")

        if event_type == "error":
            self._handle_error(event)

        elif event_type == "insight":
            self._append_jsonl("insights", event)

        elif event_type == "success":
            # Solo guardar successes marcados como noteworthy
            if event.get("metadata", {}).get("noteworthy"):
                self._handle_insight(event)

        elif event_type == "pattern":
            self._append_jsonl("patterns", event)

        elif event_type == "improvement":
            self._append_jsonl("improvements", event)

        elif event_type == "feature_request":
            self._append_jsonl("feature_requests", event)

        # Eventos success sin noteworthy se ignoran: son ruido en este nivel

    # ──────────────────────────────────────────
    #  Handlers por tipo
    # ──────────────────────────────────────────

    def _handle_error(self, event: dict) -> None:
        """Guarda el error y verifica si hay patrón recurrente."""
        self._append_jsonl("errors", event)

        # Tracking de frecuencia por (source, category)
        source = event.get("source", "unknown")
        category = event.get("category", "unknown")
        key = f"{source}:{category}"
        self._error_counts[key] += 1
        count = self._error_counts[key]

        logger.debug("Error registrado: %s (total: %d)", key, count)

        # Promoción automática a pending cuando supera el threshold
        if count >= PATTERN_THRESHOLD and key not in self._created_patterns:
            self._promote_to_pending(event, count, key)

    def _handle_insight(self, event: dict) -> None:
        """Guarda un insight positivo relevante."""
        self._append_jsonl("insights", event)
        logger.debug("Insight registrado desde '%s'.", event.get("source"))

    # ──────────────────────────────────────────
    #  Detección de patrones y promoción
    # ──────────────────────────────────────────

    def _promote_to_pending(self, event: dict, count: int, key: str) -> None:
        """
        Cuando un error supera el threshold, crea un pattern y lo pone en pending/.

        El flujo de promoción es:
            Learning (evento raw)
                ↓
            Pending (patrón detectado, esperando revisión)
                ↓
            Verified (aprobado por ReflectionCapability o humano)
                ↓
            Promoted (integrado al sistema como mejora)
        """
        self._created_patterns.add(key)

        pattern_id = f"pat-{str(uuid.uuid4())[:6]}"
        pattern = {
            "id": pattern_id,
            "timestamp": time.time(),
            "source": event.get("source"),
            "category": event.get("category"),
            "occurrences": count,
            "threshold": PATTERN_THRESHOLD,
            "key": key,
            "status": "pending",
            "triggering_event": event,
        }
        self._append_jsonl("patterns", pattern)

        # Crear archivo en pending/ (uno por patrón, no JSONL)
        pending_id = f"learning-{str(uuid.uuid4())[:8]}"
        pending_record = {
            "id": pending_id,
            "created_at": time.time(),
            "pattern_id": pattern_id,
            "source": event.get("source"),
            "category": event.get("category"),
            "action": event.get("action"),
            "occurrences": count,
            "proposed_action": self._generate_proposal(event),
            "status": "pending",
            "context_sample": event.get("context", {}),
        }
        pending_path = MEMORY_ROOT / "pending" / f"{pending_id}.json"
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(pending_record, f, ensure_ascii=False, indent=2)

        logger.info(
            "🔍 Patrón detectado → pending: [%s] x%d | Propuesta: %s",
            key, count, pending_record["proposed_action"][:80],
        )

    def _generate_proposal(self, event: dict) -> str:
        """
        Genera una propuesta de mejora heurística basada en el tipo de error.

        Para propuestas más sofisticadas, ReflectionCapability puede usar el LLM.
        """
        category = event.get("category", "")
        source = event.get("source", "")
        action = event.get("action", "")

        proposals = {
            "file_not_found":     f"Agregar validación de existencia antes de '{action}' en {source}",
            "path_traversal":     f"Revisar lógica de sanitización de rutas en {source}",
            "permission_error":   f"Mejorar manejo de permisos en {source}",
            "syntax_error":       f"Refinar EXECUTOR_PROMPT para generar Python más robusto",
            "runtime_error":      f"Agregar manejo de excepciones en el sandbox de {source}",
            "security_violation": f"Ampliar lista de imports permitidos o mejorar sandbox en {source}",
            "unknown_action":     f"Actualizar EXECUTOR_PROMPT con lista de acciones disponibles",
            "json_parse_error":   f"Agregar retry con reformateo de respuesta en el agente",
            "capability_crash":   f"Agregar circuit breaker para {source}",
            "timeout":            f"Aumentar timeout o dividir la acción en pasos menores",
        }
        return proposals.get(
            category,
            f"Revisar comportamiento de '{category}' en {source} (acción: {action})"
        )

    # ──────────────────────────────────────────
    #  Persistencia JSONL
    # ──────────────────────────────────────────

    def _append_jsonl(self, filename: str, data: dict) -> None:
        """Append-only a un archivo JSONL en learnings/."""
        path = MEMORY_ROOT / "learnings" / f"{filename}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    # ──────────────────────────────────────────
    #  API pública para ReflectionCapability y SkillBuilder
    # ──────────────────────────────────────────

    def read_errors(self, last_n: int = 100) -> list[dict]:
        return self._read_jsonl("errors", last_n)

    def read_patterns(self, last_n: int = 50) -> list[dict]:
        return self._read_jsonl("patterns", last_n)

    def read_insights(self, last_n: int = 50) -> list[dict]:
        return self._read_jsonl("insights", last_n)

    def read_improvements(self, last_n: int = 20) -> list[dict]:
        return self._read_jsonl("improvements", last_n)

    def get_pending_files(self) -> list[Path]:
        """Devuelve todos los archivos en pending/ (para el Promotion Engine)."""
        return sorted((MEMORY_ROOT / "pending").glob("*.json"))

    def get_pending_count(self) -> int:
        return len(self.get_pending_files())

    def promote(self, pending_id: str) -> bool:
        """
        Promueve un learning de pending → promoted.
        Llamado por ReflectionCapability o manualmente.
        """
        pending_path = MEMORY_ROOT / "pending" / f"{pending_id}.json"
        if not pending_path.exists():
            return False

        with open(pending_path) as f:
            record = json.load(f)
        record["status"] = "promoted"
        record["promoted_at"] = time.time()

        promoted_path = MEMORY_ROOT / "promoted" / f"{pending_id}.json"
        with open(promoted_path, "w") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        pending_path.unlink()  # Mover = copiar + borrar
        self._append_jsonl("improvements", {
            "type": "promoted_learning",
            "learning_id": pending_id,
            "source": record.get("source"),
            "proposed_action": record.get("proposed_action"),
            "timestamp": time.time(),
        })
        logger.info("Learning '%s' promovido a promoted/.", pending_id)
        return True

    def get_stats(self) -> dict:
        """Stats en RAM (no lee disco, O(1))."""
        return {
            "total_events": self._total_events,
            "events_by_type": dict(self._events_by_type),
            "unique_error_keys": len(self._error_counts),
            "top_errors": sorted(
                self._error_counts.items(), key=lambda x: -x[1]
            )[:5],
            "patterns_detected": len(self._created_patterns),
            "pending_count": self.get_pending_count(),
        }

    # ──────────────────────────────────────────
    #  Lectura genérica JSONL
    # ──────────────────────────────────────────

    def _read_jsonl(self, filename: str, last_n: int) -> list[dict]:
        path = MEMORY_ROOT / "learnings" / f"{filename}.jsonl"
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8").strip()
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
