"""
capabilities/python_capability.py — Ejecución controlada de código Python.

Dominio propio: código Python, cálculos, lógica pura.
Aprende de: SyntaxError, RuntimeError, violations de seguridad.

Memoria propia:
    workspace/capabilities/python/memory.jsonl
    → útil para detectar patrones de código que falla repetidamente.
"""

import json
import logging
import traceback
from capabilities.base_capability import BaseCapability

logger = logging.getLogger("agent.python_cap")

# Operaciones prohibidas en el sandbox
_FORBIDDEN_PATTERNS = [
    "import os", "import sys", "import subprocess", "import socket",
    "import shutil", "import pathlib", "__import__", "open(",
    "requests", "urllib", "http.client", "ftplib", "smtplib",
]

# Builtins permitidos (los más comunes sin I/O ni sistema)
_SAFE_BUILTINS = {
    "abs", "all", "any", "bin", "bool", "chr", "dict", "dir",
    "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "getattr", "hasattr", "hash", "hex", "int", "isinstance",
    "issubclass", "iter", "len", "list", "map", "max", "min",
    "next", "oct", "ord", "pow", "print", "range", "repr",
    "reversed", "round", "set", "setattr", "slice", "sorted",
    "str", "sum", "tuple", "type", "zip",
    # Excepciones comunes
    "ValueError", "TypeError", "KeyError", "IndexError",
    "AttributeError", "RuntimeError", "StopIteration",
    # Constantes
    "True", "False", "None",
}


class PythonCapability(BaseCapability):
    """
    Ejecuta código Python puro en un entorno de sandbox controlado.

    El modelo code devuelve el código como string en los params.
    Esta capability lo ejecuta, valida el resultado y emite el evento correcto.

    Aprende de:
        - SyntaxError → ajustar prompt para generar código más robusto
        - RuntimeError → agregar manejo de excepciones en el sandbox
        - security_violation → revisar lista de imports permitidos
    """

    @property
    def supported_actions(self) -> list[str]:
        return ["ejecutar_python", "calcular"]

    def execute(self, action: str, params: dict) -> tuple[bool, str]:
        if action == "ejecutar_python":
            return self._ejecutar_python(params)
        elif action == "calcular":
            return self._calcular(params)
        return False, f"ERROR: Acción no soportada por PythonCapability: '{action}'"

    def _ejecutar_python(self, params: dict) -> tuple[bool, str]:
        codigo = params.get("codigo", "")
        variable_retorno = params.get("variable_retorno", None)

        if not codigo.strip():
            return False, "ERROR: El código Python no puede estar vacío."

        # ── Validación de seguridad ─────────────
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern in codigo:
                msg = f"Operación no permitida en sandbox: '{pattern}'"
                self.emit("error", "security_violation", action="ejecutar_python",
                          context={"forbidden": pattern, "code_preview": codigo[:100]},
                          result=msg, success=False)
                self._write_memory({
                    "action": "ejecutar_python", "success": False,
                    "error": "security_violation", "forbidden": pattern,
                })
                return False, f"ERROR: {msg}"

        # ── Ejecución en sandbox ────────────────
        local_vars: dict = {}
        safe_globals = {"__builtins__": {b: __builtins__[b]  # type: ignore[index]
                                         for b in _SAFE_BUILTINS
                                         if b in (__builtins__ or {})}}
        # Fallback: si __builtins__ no es dict (CPython con -S), usar el objeto directamente
        if not safe_globals["__builtins__"]:
            safe_globals = {"__builtins__": __builtins__}

        try:
            exec(codigo, safe_globals, local_vars)  # noqa: S102
        except SyntaxError as e:
            msg = f"SyntaxError en línea {e.lineno}: {e.msg}"
            logger.warning("SyntaxError en código Python: %s", msg)
            self.emit("error", "syntax_error", action="ejecutar_python",
                      context={"line": e.lineno, "code_preview": codigo[:200]},
                      result=msg, success=False)
            self._write_memory({
                "action": "ejecutar_python", "success": False,
                "error": "syntax_error", "line": e.lineno, "details": str(e),
                "code_preview": codigo[:200],
            })
            return False, f"ERROR: {msg}"
        except Exception as e:
            tb = traceback.format_exc()
            msg = f"RuntimeError: {type(e).__name__}: {e}"
            logger.warning("RuntimeError ejecutando Python: %s", msg)
            self.emit("error", "runtime_error", action="ejecutar_python",
                      context={"exception_type": type(e).__name__, "code_preview": codigo[:200]},
                      result=msg, success=False)
            self._write_memory({
                "action": "ejecutar_python", "success": False,
                "error": "runtime_error", "exception_type": type(e).__name__,
                "details": str(e), "code_preview": codigo[:200],
            })
            return False, f"ERROR: {msg}\n{tb[:500]}"

        # ── Extraer resultado ───────────────────
        if variable_retorno and variable_retorno in local_vars:
            resultado = str(local_vars[variable_retorno])
        else:
            resultado = json.dumps(
                {k: str(v) for k, v in local_vars.items() if not k.startswith("_")},
                ensure_ascii=False,
            )

        self.emit("success", "python_executed", action="ejecutar_python",
                  context={"lines": len(codigo.splitlines())},
                  result=resultado[:200], success=True)
        self._write_memory({
            "action": "ejecutar_python", "success": True,
            "lines": len(codigo.splitlines()),
        })
        return True, resultado

    def _calcular(self, params: dict) -> tuple[bool, str]:
        """El modelo ya calculó. Aquí solo registramos y devolvemos."""
        expresion = params.get("expresion", "")
        resultado = params.get("resultado", "")
        output = f"Cálculo: {expresion} = {resultado}"
        self.emit("success", "calculation_done", action="calcular",
                  context={"expresion": expresion}, result=output, success=True)
        return True, output
