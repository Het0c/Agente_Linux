"""
capabilities/reflection_capability.py — Análisis periódico de aprendizajes.

ReflectionCapability NO es parte del flujo de ejecución.
Se invoca explícitamente:
    - Después de completar una tarea
    - Cada N tareas
    - Manualmente por el usuario

Su rol:
    1. Lee todos los JSONL de learnings/
    2. Encuentra patrones de frecuencia (top errores, acciones problemáticas)
    3. Lee pending/ y decide qué promover
    4. Opcionalmente usa el LLM para análisis más profundo
    5. Escribe propuestas a improvements.jsonl
    6. Imprime un reporte en consola

El resultado de la reflexión alimenta a SkillBuilder para
la creación automática de nuevas capabilities.
"""

import json
import time
import logging
from pathlib import Path
from collections import Counter
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from capabilities.learning_capability import LearningCapability
    from model_manager import ModelManager

logger = logging.getLogger("agent.reflection")

MEMORY_ROOT = Path("workspace/memory")

# Threshold para auto-promoción de learnings durante la reflexión
AUTO_PROMOTE_THRESHOLD = 5  # Si un error tiene >= 5 ocurrencias → promover


class ReflectionCapability:
    """
    Analiza los datos acumulados en learnings/ y produce mejoras concretas.

    Puede funcionar en dos modos:
        - Heurístico (sin LLM): análisis de frecuencias y patrones simples.
        - LLM-powered (con model_manager): análisis más rico en lenguaje natural.
    """

    def __init__(
        self,
        learning: Optional["LearningCapability"] = None,
        model_manager: Optional["ModelManager"] = None,
    ):
        self.learning = learning
        self.model_manager = model_manager  # Opcional para análisis LLM

    # ──────────────────────────────────────────
    #  API principal
    # ──────────────────────────────────────────

    def reflect(self, verbose: bool = True) -> dict:
        """
        Ejecuta un ciclo completo de reflexión.

        Returns:
            Dict con el análisis completo.
        """
        logger.info("=== Iniciando ciclo de reflexión ===")

        errors = self._read_jsonl("errors")
        patterns = self._read_jsonl("patterns")
        insights = self._read_jsonl("insights")
        pending_files = list((MEMORY_ROOT / "pending").glob("*.json")) if MEMORY_ROOT.joinpath("pending").exists() else []

        analysis = {
            "timestamp": time.time(),
            "summary": {
                "total_errors": len(errors),
                "total_patterns": len(patterns),
                "total_insights": len(insights),
                "pending_learnings": len(pending_files),
            },
            "top_errors": self._top_occurrences(errors, field="category"),
            "top_error_sources": self._top_occurrences(errors, field="source"),
            "top_failing_actions": self._top_occurrences(errors, field="action"),
            "patterns_summary": self._summarize_patterns(patterns),
            "pending_review": self._review_pending(pending_files),
            "improvements": [],
            "llm_analysis": None,
        }

        # Generar propuestas de mejora
        analysis["improvements"] = self._generate_improvements(analysis)

        # Análisis LLM opcional
        if self.model_manager and errors:
            analysis["llm_analysis"] = self._llm_analysis(analysis)

        # Persistir el análisis
        self._save_analysis(analysis)

        if verbose:
            self._print_report(analysis)

        logger.info("Reflexión completada. %d mejoras propuestas.", len(analysis["improvements"]))
        return analysis

    # ──────────────────────────────────────────
    #  Análisis heurístico
    # ──────────────────────────────────────────

    def _top_occurrences(self, records: list[dict], field: str, top_n: int = 5) -> list[dict]:
        """Cuenta ocurrencias de un campo y devuelve el top N."""
        counts = Counter(r.get(field, "unknown") for r in records if r.get(field))
        return [{"value": v, "count": c} for v, c in counts.most_common(top_n)]

    def _summarize_patterns(self, patterns: list[dict]) -> list[dict]:
        """Resume los patrones detectados por LearningCapability."""
        return [
            {
                "key": p.get("key"),
                "occurrences": p.get("occurrences"),
                "status": p.get("status"),
                "source": p.get("source"),
                "category": p.get("category"),
            }
            for p in patterns[-10:]  # Últimos 10 patrones
        ]

    def _review_pending(self, pending_files: list[Path]) -> list[dict]:
        """
        Lee todos los pending y decide cuáles promover automáticamente.

        Auto-promueve los que tienen alta frecuencia de ocurrencias.
        Los demás se listan para revisión manual.
        """
        review = []
        for f in pending_files:
            try:
                with open(f) as fp:
                    record = json.load(fp)
                occurrences = record.get("occurrences", 0)
                auto_promote = occurrences >= AUTO_PROMOTE_THRESHOLD

                if auto_promote and self.learning:
                    promoted = self.learning.promote(record["id"])
                    record["auto_promoted"] = promoted
                else:
                    record["auto_promoted"] = False

                review.append({
                    "id": record["id"],
                    "source": record.get("source"),
                    "category": record.get("category"),
                    "occurrences": occurrences,
                    "proposed_action": record.get("proposed_action"),
                    "auto_promoted": record.get("auto_promoted", False),
                })
            except Exception as e:
                logger.debug("Error leyendo pending file %s: %s", f.name, e)
        return review

    def _generate_improvements(self, analysis: dict) -> list[dict]:
        """Genera propuestas de mejora basadas en el análisis heurístico."""
        improvements = []

        # Propuesta por error más frecuente
        if analysis["top_errors"]:
            top = analysis["top_errors"][0]
            if top["count"] >= 3:
                improvements.append({
                    "type": "fix_recurring_error",
                    "priority": "high",
                    "description": f"Error '{top['value']}' ocurrió {top['count']} veces.",
                    "action": f"Revisar y corregir manejo de '{top['value']}'.",
                    "timestamp": time.time(),
                })

        # Propuesta por fuente con más errores
        if analysis["top_error_sources"]:
            source = analysis["top_error_sources"][0]
            if source["count"] >= 5:
                improvements.append({
                    "type": "capability_improvement",
                    "priority": "medium",
                    "description": f"'{source['value']}' tiene {source['count']} errores acumulados.",
                    "action": f"Revisar robustez de {source['value']}.",
                    "timestamp": time.time(),
                })

        # Propuesta por learnings pendientes sin promover
        pending_count = analysis["summary"]["pending_learnings"]
        if pending_count >= 3:
            improvements.append({
                "type": "review_pending",
                "priority": "low",
                "description": f"Hay {pending_count} learnings pendientes de revisión.",
                "action": "Ejecutar ReflectionCapability.reflect() o revisar workspace/memory/pending/",
                "timestamp": time.time(),
            })

        return improvements

    # ──────────────────────────────────────────
    #  Análisis LLM (opcional)
    # ──────────────────────────────────────────

    def _llm_analysis(self, analysis: dict) -> Optional[str]:
        """
        Usa el modelo agent para generar un análisis en lenguaje natural
        de los patrones detectados.

        Solo se llama si model_manager está disponible.
        """
        if self.model_manager is None:
            return None

        summary_str = json.dumps({
            "top_errors": analysis["top_errors"][:3],
            "top_failing_actions": analysis["top_failing_actions"][:3],
            "pending_count": analysis["summary"]["pending_learnings"],
        }, ensure_ascii=False, indent=2)

        prompt = (
            f"Eres un analista de sistemas de agentes IA.\n"
            f"Analiza estos datos de errores y genera un diagnóstico breve (máx 150 palabras):\n\n"
            f"{summary_str}\n\n"
            f"Identifica: (1) el problema principal, (2) la causa probable, (3) la mejora más urgente."
        )

        try:
            self.model_manager.load_model("agent")
            respuesta = self.model_manager.infer(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.3,
            )
            return respuesta.strip()
        except Exception as e:
            logger.warning("Análisis LLM falló: %s", e)
            return None
        finally:
            self.model_manager.unload_model()

    # ──────────────────────────────────────────
    #  Persistencia y reporte
    # ──────────────────────────────────────────

    def _save_analysis(self, analysis: dict) -> None:
        """Guarda el análisis en improvements.jsonl."""
        path = MEMORY_ROOT / "learnings" / "improvements.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(analysis, ensure_ascii=False) + "\n")

    def _print_report(self, analysis: dict) -> None:
        """Imprime el reporte de reflexión en consola."""
        s = analysis["summary"]
        print("\n" + "═" * 60)
        print("  🔍 REPORTE DE REFLEXIÓN")
        print("═" * 60)
        print(f"  Errores totales:    {s['total_errors']}")
        print(f"  Patrones detectados:{s['total_patterns']}")
        print(f"  Insights positivos: {s['total_insights']}")
        print(f"  Learnings pendientes:{s['pending_learnings']}")

        if analysis["top_errors"]:
            print("\n  ─ Top errores ──────────────────────────")
            for e in analysis["top_errors"][:3]:
                print(f"    {e['count']}x  {e['value']}")

        if analysis["improvements"]:
            print("\n  ─ Mejoras propuestas ───────────────────")
            for imp in analysis["improvements"]:
                prio = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(imp["priority"], "·")
                print(f"    {prio} [{imp['type']}] {imp['action'][:70]}")

        if analysis.get("llm_analysis"):
            print("\n  ─ Análisis LLM ─────────────────────────")
            for line in analysis["llm_analysis"].splitlines():
                print(f"    {line}")

        pending_promoted = [r for r in analysis.get("pending_review", []) if r.get("auto_promoted")]
        if pending_promoted:
            print(f"\n  ✅ Auto-promovidos: {len(pending_promoted)} learnings")

        print("═" * 60 + "\n")

    def _read_jsonl(self, filename: str) -> list[dict]:
        path = MEMORY_ROOT / "learnings" / f"{filename}.jsonl"
        if not path.exists():
            return []
        try:
            lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
            records = []
            for line in lines:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            return records
        except Exception:
            return []
