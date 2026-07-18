"""
capabilities/filesystem_capability.py — Operaciones de sistema de archivos.

Diferencias clave respecto al executor.py original:
  - Cada operación emite eventos al bus (éxito Y error).
  - Cada error se registra en su propia memoria JSONL.
  - La validación de rutas vive aquí, no en el executor.
  - Es introspectable: sabe cuántos errores ha tenido y de qué tipo.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from capabilities.base_capability import BaseCapability

if TYPE_CHECKING:
    from capabilities.event_bus import EventBus

logger = logging.getLogger("agent.filesystem")

# Directorio raíz de outputs del agente (sandbox de escritura)
AGENT_WORKSPACE = Path("agent_workspace")


class FilesystemCapability(BaseCapability):
    """
    Maneja todas las operaciones de sistema de archivos.

    Seguridad:
        - Path traversal prevenido en _validate_path().
        - Todas las rutas se resuelven dentro de AGENT_WORKSPACE.
        - Rutas absolutas son rechazadas o contenidas.

    Memoria propia:
        workspace/capabilities/filesystem/memory.jsonl
        → útil para ver qué rutas han causado errores repetidamente.
    """

    @property
    def supported_actions(self) -> list[str]:
        return [
            "crear_archivo",
            "leer_archivo",
            "escribir_archivo",
            "append_archivo",
            "crear_directorio",
        ]

    def execute(self, action: str, params: dict) -> tuple[bool, str]:
        handlers = {
            "crear_archivo":   self._crear_archivo,
            "leer_archivo":    self._leer_archivo,
            "escribir_archivo": self._escribir_archivo,
            "append_archivo":  self._append_archivo,
            "crear_directorio": self._crear_directorio,
        }
        handler = handlers.get(action)
        if handler is None:
            return False, f"ERROR: Acción no soportada por FilesystemCapability: '{action}'"

        try:
            return handler(params)
        except Exception as e:
            msg = f"Error inesperado en {action}: {e}"
            logger.exception(msg)
            self.emit(
                "error", "unexpected_error",
                action=action, context=params,
                result=msg, success=False,
            )
            self._write_memory({"action": action, "success": False, "error": "unexpected", "details": str(e)})
            return False, f"ERROR: {msg}"

    # ──────────────────────────────────────────
    #  Handlers de acciones
    # ──────────────────────────────────────────

    def _crear_archivo(self, params: dict) -> tuple[bool, str]:
        ruta_str = params.get("ruta", "output/resultado.txt")
        contenido = params.get("contenido", "")

        ruta, err = self._validate_path(ruta_str)
        if ruta is None:
            self._emit_path_error("crear_archivo", ruta_str, err)
            return False, f"ERROR: {err}"

        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido, encoding="utf-8")
        resultado = f"Archivo creado: {ruta} ({len(contenido)} chars)"

        self.emit("success", "file_created", action="crear_archivo",
                  context={"ruta": ruta_str, "size": len(contenido)},
                  result=resultado, success=True)
        self._write_memory({"action": "crear_archivo", "ruta": ruta_str, "success": True})
        return True, resultado

    def _leer_archivo(self, params: dict) -> tuple[bool, str]:
        ruta_str = params.get("ruta", "")

        ruta, err = self._validate_path(ruta_str)
        if ruta is None:
            self._emit_path_error("leer_archivo", ruta_str, err)
            return False, f"ERROR: {err}"

        if not ruta.exists():
            msg = f"Archivo no encontrado: {ruta_str}"
            self.emit("error", "file_not_found", action="leer_archivo",
                      context={"ruta": ruta_str}, result=msg, success=False)
            self._write_memory({"action": "leer_archivo", "ruta": ruta_str,
                                "success": False, "error": "file_not_found"})
            return False, f"ERROR: {msg}"

        contenido = ruta.read_text(encoding="utf-8")
        self.emit("success", "file_read", action="leer_archivo",
                  context={"ruta": ruta_str, "size": len(contenido)}, success=True)
        return True, contenido[:4000]  # Límite para no saturar el contexto

    def _escribir_archivo(self, params: dict) -> tuple[bool, str]:
        ruta_str = params.get("ruta", "")
        contenido = params.get("contenido", "")

        ruta, err = self._validate_path(ruta_str)
        if ruta is None:
            self._emit_path_error("escribir_archivo", ruta_str, err)
            return False, f"ERROR: {err}"

        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido, encoding="utf-8")
        resultado = f"Archivo escrito: {ruta} ({len(contenido)} chars)"

        self.emit("success", "file_written", action="escribir_archivo",
                  context={"ruta": ruta_str}, result=resultado, success=True)
        self._write_memory({"action": "escribir_archivo", "ruta": ruta_str, "success": True})
        return True, resultado

    def _append_archivo(self, params: dict) -> tuple[bool, str]:
        ruta_str = params.get("ruta", "")
        contenido = params.get("contenido", "")

        ruta, err = self._validate_path(ruta_str)
        if ruta is None:
            self._emit_path_error("append_archivo", ruta_str, err)
            return False, f"ERROR: {err}"

        ruta.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(contenido)
        resultado = f"Contenido agregado a: {ruta} ({len(contenido)} chars)"

        self.emit("success", "file_appended", action="append_archivo",
                  context={"ruta": ruta_str}, result=resultado, success=True)
        return True, resultado

    def _crear_directorio(self, params: dict) -> tuple[bool, str]:
        ruta_str = params.get("ruta", "")

        ruta, err = self._validate_path(ruta_str)
        if ruta is None:
            self._emit_path_error("crear_directorio", ruta_str, err)
            return False, f"ERROR: {err}"

        ruta.mkdir(parents=True, exist_ok=True)
        resultado = f"Directorio creado: {ruta}"

        self.emit("success", "directory_created", action="crear_directorio",
                  context={"ruta": ruta_str}, result=resultado, success=True)
        return True, resultado

    # ──────────────────────────────────────────
    #  Validación y helpers
    # ──────────────────────────────────────────

    def _validate_path(self, ruta_str: str) -> tuple[Path | None, str]:
        """
        Valida y normaliza una ruta dentro del workspace del agente.

        Previene:
          - Rutas vacías
          - Path traversal (../../etc/passwd)
          - Acceso fuera de AGENT_WORKSPACE

        Returns:
            (Path resuelto, "") si es válida.
            (None, mensaje_error) si es inválida.
        """
        if not ruta_str or not ruta_str.strip():
            return None, "La ruta no puede estar vacía."

        AGENT_WORKSPACE.mkdir(exist_ok=True)
        base = AGENT_WORKSPACE.resolve()

        ruta = Path(ruta_str.strip())
        # Contener rutas absolutas dentro del workspace
        if ruta.is_absolute():
            ruta = Path(*ruta.parts[1:]) if len(ruta.parts) > 1 else Path(ruta.name)

        ruta_final = (base / ruta).resolve()

        if not str(ruta_final).startswith(str(base)):
            return None, f"Path traversal detectado: '{ruta_str}' queda fuera del workspace."

        return ruta_final, ""

    def _emit_path_error(self, action: str, ruta: str, msg: str) -> None:
        category = "path_traversal" if "traversal" in msg.lower() else "invalid_path"
        self.emit("error", category, action=action,
                  context={"ruta": ruta}, result=msg, success=False)
        self._write_memory({"action": action, "ruta": ruta,
                            "success": False, "error": category})
