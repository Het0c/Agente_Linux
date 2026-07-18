"""
capabilities/skill_builder.py — Convierte patrones recurrentes en propuestas de nuevas capabilities.

Cuando el sistema detecta que el mismo error ocurre >= SKILL_THRESHOLD veces,
SkillBuilder extrae el patrón y crea una propuesta estructurada de:
    - Nueva capability
    - Mejora a una capability existente
    - Nuevo handler de acción

El output va a:
    workspace/memory/pending/skill-{id}.json    → para revisión
    workspace/skills/proposed/skill-{id}.json   → esqueleto de código

Este es el cierre del Capability Lifecycle:
    Observa → Aprende → Reflexiona → Propone mejora → Valida → Actualiza capability

SkillBuilder implementa los pasos "Propone mejora" y "Actualiza capability".
"""

import json
import uuid
import time
import logging
from collections import Counter
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from capabilities.learning_capability import LearningCapability
    from model_manager import ModelManager

logger = logging.getLogger("agent.skill_builder")

MEMORY_ROOT = Path("workspace/memory")
SKILLS_ROOT = Path("workspace/skills/proposed")

# Umbral de errores para proponer una nueva skill
SKILL_THRESHOLD = 15


class SkillBuilder:
    """
    Extractor automático de skills desde patrones de errores recurrentes.

    Flujo:
        scan_and_build()
            ↓
        Leer errors.jsonl
            ↓
        Contar por (source, category)
            ↓
        Si count >= SKILL_THRESHOLD:
            ↓
        _create_skill_proposal(...)
            ↓
        Escribir en workspace/skills/proposed/ y workspace/memory/pending/

    Las propuestas pueden ser:
        - "new_capability": crear una clase nueva
        - "new_action": agregar un handler a una capability existente
        - "improve_validation": mejorar la validación de inputs
        - "improve_prompt": ajustar un prompt del sistema
    """

    def __init__(
        self,
        learning: Optional["LearningCapability"] = None,
        model_manager: Optional["ModelManager"] = None,
    ):
        self.learning = learning
        self.model_manager = model_manager
        SKILLS_ROOT.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────
    #  API principal
    # ──────────────────────────────────────────

    def scan_and_build(self) -> list[dict]:
        """
        Escanea learnings y genera propuestas de skills para patrones frecuentes.

        Returns:
            Lista de propuestas generadas en este ciclo.
        """
        if self.learning is None:
            logger.warning("SkillBuilder sin LearningCapability. Leyendo JSONL directamente.")

        errors = self._load_errors()
        if not errors:
            logger.info("Sin errores registrados. Nada que analizar.")
            return []

        # Contar por (source, category)
        pattern_counts = Counter(
            f"{e.get('source', 'unknown')}:{e.get('category', 'unknown')}"
            for e in errors
        )

        proposals = []
        already_proposed = self._load_already_proposed()

        for key, count in pattern_counts.most_common():
            if count < SKILL_THRESHOLD:
                break  # Counter está ordenado, ya no hay más con count >= threshold
            if key in already_proposed:
                logger.debug("Patrón '%s' ya tiene propuesta. Saltando.", key)
                continue

            source, category = key.split(":", 1)
            sample_errors = [e for e in errors if
                             e.get("source") == source and e.get("category") == category][-5:]

            proposal = self._create_skill_proposal(key, count, source, category, sample_errors)
            if proposal:
                proposals.append(proposal)
                already_proposed.add(key)

        logger.info("SkillBuilder: %d propuestas generadas.", len(proposals))
        return proposals

    # ──────────────────────────────────────────
    #  Creación de propuestas
    # ──────────────────────────────────────────

    def _create_skill_proposal(
        self,
        key: str,
        count: int,
        source: str,
        category: str,
        sample_errors: list[dict],
    ) -> Optional[dict]:
        """
        Genera una propuesta estructurada de skill y la persiste.

        Args:
            key: "source:category"
            count: Número de ocurrencias
            source: Nombre de la capability origen
            category: Tipo de error
            sample_errors: Muestra de errores para análisis

        Returns:
            Dict con la propuesta, o None si no se pudo crear.
        """
        skill_type = self._classify_skill_type(source, category)
        skill_id = f"skill-{str(uuid.uuid4())[:8]}"

        proposal = {
            "id": skill_id,
            "created_at": time.time(),
            "trigger": {
                "key": key,
                "source": source,
                "category": category,
                "occurrences": count,
                "threshold": SKILL_THRESHOLD,
            },
            "skill_type": skill_type,
            "title": self._generate_title(source, category, skill_type),
            "description": self._generate_description(source, category, count, skill_type),
            "proposed_code": self._generate_code_skeleton(source, category, skill_type),
            "sample_errors": sample_errors,
            "status": "proposed",
            "llm_refinement": None,
        }

        # Enriquecer con LLM si está disponible
        if self.model_manager:
            proposal["llm_refinement"] = self._llm_refine(proposal)

        # Guardar en workspace/skills/proposed/
        skill_path = SKILLS_ROOT / f"{skill_id}.json"
        with open(skill_path, "w", encoding="utf-8") as f:
            json.dump(proposal, f, ensure_ascii=False, indent=2)

        # También poner en pending/ para el Promotion Engine
        pending_path = MEMORY_ROOT / "pending" / f"{skill_id}.json"
        pending_record = {
            "id": skill_id,
            "type": "skill_proposal",
            "created_at": time.time(),
            "source": source,
            "category": category,
            "occurrences": count,
            "proposed_action": proposal["title"],
            "skill_path": str(skill_path),
            "status": "pending",
        }
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(pending_record, f, ensure_ascii=False, indent=2)

        logger.info(
            "🔧 Skill propuesta: [%s] '%s' (trigger: %s x%d)",
            skill_type, proposal["title"], key, count,
        )
        return proposal

    # ──────────────────────────────────────────
    #  Clasificación del tipo de skill
    # ──────────────────────────────────────────

    def _classify_skill_type(self, source: str, category: str) -> str:
        """
        Determina qué tipo de mejora se necesita.

        Tipos:
            new_capability      → error de dominio sin capability propia
            new_action          → falta un handler de acción
            improve_validation  → fallos repetidos de validación de inputs
            improve_prompt      → el modelo genera acciones incorrectas
            improve_sandbox     → fallos de ejecución en sandbox
        """
        if category in ("unknown_action",):
            return "new_action"
        if category in ("syntax_error", "runtime_error"):
            return "improve_sandbox"
        if category in ("file_not_found", "path_traversal", "invalid_path", "permission_error"):
            return "improve_validation"
        if category in ("json_parse_error", "unknown_action"):
            return "improve_prompt"
        if "Capability" not in source:
            return "new_capability"
        return "improve_capability"

    # ──────────────────────────────────────────
    #  Generación de contenido de la propuesta
    # ──────────────────────────────────────────

    def _generate_title(self, source: str, category: str, skill_type: str) -> str:
        titles = {
            "new_action": f"Agregar handler para acción desconocida detectada en {source}",
            "improve_sandbox": f"Mejorar sandbox de ejecución en {source} (reduce {category})",
            "improve_validation": f"Reforzar validación de inputs en {source} (reduce {category})",
            "improve_prompt": f"Actualizar prompt para eliminar '{category}' repetido",
            "new_capability": f"Crear {source}Capability para manejar '{category}'",
            "improve_capability": f"Mejorar {source} para manejar '{category}' correctamente",
        }
        return titles.get(skill_type, f"Mejora para {source}:{category}")

    def _generate_description(self, source: str, category: str, count: int, skill_type: str) -> str:
        return (
            f"El error '{category}' en '{source}' ocurrió {count} veces "
            f"(threshold: {SKILL_THRESHOLD}). "
            f"Tipo de mejora sugerida: {skill_type}. "
            f"Esta propuesta fue generada automáticamente por SkillBuilder."
        )

    def _generate_code_skeleton(self, source: str, category: str, skill_type: str) -> str:
        """Genera un esqueleto de código Python como punto de partida."""

        if skill_type == "improve_validation":
            return f'''\
# Mejora sugerida: validación adicional para '{category}' en {source}
# Agregar al método execute() o al handler afectado:

def _validate_{category.replace("-", "_")}(self, params: dict) -> tuple[bool, str]:
    """
    Validación adicional generada por SkillBuilder.
    Previene el error '{category}' detectado {SKILL_THRESHOLD}+ veces.
    """
    # TODO: implementar validación específica
    # Ejemplo para file_not_found:
    #   ruta = params.get("ruta", "")
    #   if not Path(ruta).exists():
    #       return False, f"Archivo no encontrado: {{ruta}}"
    return True, ""
'''

        elif skill_type == "new_action":
            action_name = category.replace("_", "-") if "_" in category else f"nueva_accion"
            return f'''\
# Nueva acción sugerida: detectada como unknown_action en {source}
# Agregar a la capability apropiada:

# En la capability destino, agregar a supported_actions:
#   return [..., "{action_name}"]

def handle_{action_name.replace("-", "_")}(self, params: dict) -> tuple[bool, str]:
    """
    Handler para '{action_name}'.
    Generado por SkillBuilder por detección de unknown_action.
    """
    # TODO: implementar lógica
    return True, "Acción ejecutada"

# También actualizar EXECUTOR_PROMPT en utils.py para incluir esta acción.
'''

        elif skill_type == "improve_sandbox":
            return f'''\
# Mejora del sandbox para reducir '{category}' en {source}
# Opciones según el tipo:

# Para SyntaxError: mejorar el prompt para generar código más robusto
# En utils.py, EXECUTOR_PROMPT, agregar:
#   "Siempre verifica que el código Python sea sintácticamente correcto."
#   "Usa try/except para operaciones que puedan fallar."

# Para RuntimeError: agregar manejo de excepciones en el sandbox
# En python_capability.py:
#   try:
#       exec(codigo, safe_globals, local_vars)
#   except {category.replace("_error", "Error").title()} as e:
#       # Handler específico
#       ...
'''

        else:
            return f'''\
# Esqueleto de mejora para '{skill_type}' en {source}
# Categoría de error: {category}
# TODO: implementar mejora específica basada en los sample_errors del JSON de propuesta
'''

    # ──────────────────────────────────────────
    #  Refinamiento con LLM (opcional)
    # ──────────────────────────────────────────

    def _llm_refine(self, proposal: dict) -> Optional[str]:
        """Usa el modelo agent para refinar la propuesta con lenguaje natural."""
        if self.model_manager is None:
            return None

        prompt = (
            f"Eres un arquitecto de sistemas de agentes IA.\n"
            f"Se detectó el siguiente patrón de errores recurrentes:\n\n"
            f"  Fuente: {proposal['trigger']['source']}\n"
            f"  Categoría: {proposal['trigger']['category']}\n"
            f"  Ocurrencias: {proposal['trigger']['occurrences']}\n\n"
            f"Propuesta actual: {proposal['title']}\n\n"
            f"En 3 oraciones, describe cómo implementar esta mejora de forma óptima."
        )

        try:
            self.model_manager.load_model("agent")
            return self.model_manager.infer(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.4,
            ).strip()
        except Exception as e:
            logger.warning("LLM refinement falló: %s", e)
            return None
        finally:
            self.model_manager.unload_model()

    # ──────────────────────────────────────────
    #  Helpers
    # ──────────────────────────────────────────

    def _load_errors(self) -> list[dict]:
        if self.learning:
            return self.learning.read_errors(last_n=500)

        # Fallback: leer JSONL directamente
        path = MEMORY_ROOT / "learnings" / "errors.jsonl"
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def _load_already_proposed(self) -> set[str]:
        """Carga los patrones para los que ya existe una propuesta (evita duplicados)."""
        proposed = set()
        for f in SKILLS_ROOT.glob("*.json"):
            try:
                with open(f) as fp:
                    d = json.load(fp)
                key = d.get("trigger", {}).get("key")
                if key:
                    proposed.add(key)
            except Exception:
                pass
        return proposed

    def list_proposals(self) -> list[dict]:
        """Lista todas las propuestas existentes con su estado."""
        proposals = []
        for f in sorted(SKILLS_ROOT.glob("*.json")):
            try:
                with open(f) as fp:
                    d = json.load(fp)
                proposals.append({
                    "id": d.get("id"),
                    "title": d.get("title"),
                    "skill_type": d.get("skill_type"),
                    "occurrences": d.get("trigger", {}).get("occurrences"),
                    "status": d.get("status"),
                    "file": str(f),
                })
            except Exception:
                pass
        return proposals
