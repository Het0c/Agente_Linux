"""
executor.py — Ejecutor desacoplado de acciones.

El modelo de código devuelve acciones ESTRUCTURADAS (JSON).
Python ejecuta esas acciones aquí, con validación y seguridad.

El LLM nunca ejecuta código directamente.
"""

import os
import json
import logging
import traceback
from pathlib import Path
from typing import Any, Optional

from model_manager import ModelManager
from utils import EXECUTOR_PROMPT, parse_json_response, print_section

logger = logging.getLogger("agent.executor")


# ──────────────────────────────────────────────
#  Registro de "skills" (acciones disponibles)
# ──────────────────────────────────────────────
#
# Para añadir nuevas skills en el futuro:
#   1. Implementar una función handle_<nombre>() abajo.
#   2. Registrarla en ACTION_REGISTRY.
#   3. Documentar su schema en EXECUTOR_PROMPT (en utils.py).

ACTION_REGISTRY: dict[str, callable] = {}  # se llena con @register_action


def register_action(name: str):
    """Decorador para registrar handlers de acciones."""
    def decorator(fn):
        ACTION_REGISTRY[name] = fn
        logger.debug("Acción registrada: '%s'", name)
        return fn
    return decorator


# ══════════════════════════════════════════════
#  Handlers de acciones
# ══════════════════════════════════════════════

@register_action("crear_archivo")
def handle_crear_archivo(params: dict) -> str:
    ruta = _validate_path(params.get("ruta", "output/resultado.txt"))
    contenido = params.get("contenido", "")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return f"Archivo creado: {ruta} ({len(contenido)} chars)"


@register_action("leer_archivo")
def handle_leer_archivo(params: dict) -> str:
    ruta = _validate_path(params.get("ruta", ""))
    if not ruta.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {ruta}")
    contenido = ruta.read_text(encoding="utf-8")
    return contenido[:4000]  # Límite para no saturar el contexto


@register_action("escribir_archivo")
def handle_escribir_archivo(params: dict) -> str:
    ruta = _validate_path(params.get("ruta", ""))
    contenido = params.get("contenido", "")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")
    return f"Archivo escrito: {ruta} ({len(contenido)} chars)"


@register_action("append_archivo")
def handle_append_archivo(params: dict) -> str:
    ruta = _validate_path(params.get("ruta", ""))
    contenido = params.get("contenido", "")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "a", encoding="utf-8") as f:
        f.write(contenido)
    return f"Contenido agregado a: {ruta} ({len(contenido)} chars)"


@register_action("crear_directorio")
def handle_crear_directorio(params: dict) -> str:
    ruta = _validate_path(params.get("ruta", ""))
    ruta.mkdir(parents=True, exist_ok=True)
    return f"Directorio creado: {ruta}"


@register_action("ejecutar_python")
def handle_ejecutar_python(params: dict) -> str:
    """
    Ejecuta un fragmento de código Python en un entorno controlado.

    SEGURIDAD: Solo permite operaciones seguras. No permite imports peligrosos,
    acceso a red, ni comandos del sistema.
    """
    codigo = params.get("codigo", "")
    variable_retorno = params.get("variable_retorno", None)

    # Lista blanca de imports permitidos
    _ALLOWED_IMPORTS = {"math", "json", "re", "datetime", "collections",
                        "itertools", "functools", "string", "random"}
    _FORBIDDEN = ["import os", "import sys", "import subprocess", "import socket",
                  "__import__", "eval(", "exec(", "open(", "requests"]

    for forbidden in _FORBIDDEN:
        if forbidden in codigo:
            raise PermissionError(
                f"El código contiene una operación no permitida: '{forbidden}'"
            )

    local_vars: dict[str, Any] = {}
    try:
        exec(codigo, {"__builtins__": __builtins__}, local_vars)  # noqa: S102
    except Exception as e:
        raise RuntimeError(f"Error ejecutando código Python: {e}\n{traceback.format_exc()}") from e

    if variable_retorno and variable_retorno in local_vars:
        return str(local_vars[variable_retorno])

    # Si no hay variable de retorno, devuelve todas las variables locales no privadas
    resultado = {k: str(v) for k, v in local_vars.items() if not k.startswith("_")}
    return json.dumps(resultado, ensure_ascii=False)


@register_action("calcular")
def handle_calcular(params: dict) -> str:
    """Acción de cálculo: el modelo ya calculó, solo se registra."""
    expresion = params.get("expresion", "")
    resultado = params.get("resultado", "")
    return f"Cálculo: {expresion} = {resultado}"


@register_action("responder")
def handle_responder(params: dict) -> str:
    """Acción de respuesta directa al usuario."""
    return params.get("mensaje", "")


# ══════════════════════════════════════════════
#  Clase principal del ejecutor
# ══════════════════════════════════════════════

class Executor:
    """
    Coordina la ejecución de pasos del plan:
      1. Carga el modelo code.
      2. Genera una acción estructurada via LLM.
      3. Parsea la acción.
      4. Ejecuta la acción en Python.
      5. Descarga el modelo code.
    """

    def __init__(self, model_manager: ModelManager, debug: bool = False):
        self.mm = model_manager
        self.debug = debug

    def ejecutar_paso(self, paso: str, contexto: dict) -> tuple[bool, str]:
        """
        Ejecuta un paso del plan.

        Args:
            paso: Descripción del paso a ejecutar.
            contexto: Contexto relevante del estado del agente.

        Returns:
            (exitoso: bool, resultado: str)
        """
        print_section("Ejecutor")
        logger.info("Ejecutando paso: %s", paso[:100])

        # 1. Pedir al modelo code que genere la acción
        accion_dict = self._generar_accion(paso, contexto)
        if accion_dict is None:
            return False, "ERROR: El modelo no generó una acción válida."

        nombre_accion = accion_dict.get("accion", "")
        parametros = accion_dict.get("parametros", {})
        razon = accion_dict.get("razon", "")

        logger.info("Acción generada: '%s' | Razón: %s", nombre_accion, razon[:80])
        if self.debug:
            logger.debug("Parámetros: %s", json.dumps(parametros, ensure_ascii=False)[:300])

        # 2. Ejecutar la acción en Python
        return self._ejecutar_accion(nombre_accion, parametros)

    def ejecutar_simple(self, objetivo: str) -> tuple[bool, str]:
        """
        Ejecuta una tarea simple directamente, sin plan.

        Args:
            objetivo: Objetivo completo de la tarea.

        Returns:
            (exitoso: bool, resultado: str)
        """
        contexto = {"objetivo": objetivo, "modo": "simple"}
        return self.ejecutar_paso(objetivo, contexto)

    # ──────────────────────────────────────────
    #  Internos
    # ──────────────────────────────────────────

    def _generar_accion(self, paso: str, contexto: dict) -> Optional[dict]:
        """Llama al modelo code para obtener la acción estructurada."""
        self.mm.load_model("code")

        contexto_str = json.dumps(contexto, ensure_ascii=False, indent=2)
        mensaje_usuario = (
            f"CONTEXTO:\n{contexto_str}\n\n"
            f"PASO A EJECUTAR:\n{paso}\n\n"
            f"Genera la acción estructurada para completar este paso."
        )

        try:
            respuesta = self.mm.infer(
                messages=[{"role": "user", "content": mensaje_usuario}],
                system_prompt=EXECUTOR_PROMPT,
            )
        except RuntimeError as e:
            logger.error("Error en inferencia del modelo code: %s", e)
            return None
        finally:
            self.mm.unload_model()

        accion = parse_json_response(respuesta, context="executor")
        if accion is None:
            logger.warning("Respuesta del modelo code no es JSON válido: %s", respuesta[:200])
        return accion

    def _ejecutar_accion(self, nombre: str, parametros: dict) -> tuple[bool, str]:
        """Busca y ejecuta el handler de la acción."""
        if nombre not in ACTION_REGISTRY:
            msg = (
                f"Acción desconocida: '{nombre}'. "
                f"Disponibles: {list(ACTION_REGISTRY.keys())}"
            )
            logger.error(msg)
            return False, f"ERROR: {msg}"

        handler = ACTION_REGISTRY[nombre]
        try:
            resultado = handler(parametros)
            logger.info("Acción '%s' ejecutada OK. Resultado: %s", nombre, str(resultado)[:100])
            return True, str(resultado)
        except (PermissionError, FileNotFoundError) as e:
            msg = f"Error de seguridad/archivo en acción '{nombre}': {e}"
            logger.error(msg)
            return False, f"ERROR: {msg}"
        except Exception as e:
            msg = f"Error inesperado en acción '{nombre}': {e}"
            logger.exception(msg)
            return False, f"ERROR: {msg}"


# ──────────────────────────────────────────────
#  Helpers internos
# ──────────────────────────────────────────────

def _validate_path(ruta_str: str) -> Path:
    """
    Valida y normaliza una ruta de archivo.

    Previene path traversal (../../etc/passwd, etc.).
    Todas las rutas se resuelven dentro del directorio de trabajo.
    """
    if not ruta_str:
        raise ValueError("La ruta no puede estar vacía.")

    # Directorio base de trabajo (donde se guardan los outputs del agente)
    base_dir = Path("agent_workspace").resolve()
    base_dir.mkdir(exist_ok=True)

    # Si la ruta es absoluta, la convertimos a relativa para contenerla
    ruta = Path(ruta_str)
    if ruta.is_absolute():
        # Tomar solo la parte relativa (quitar la raíz)
        ruta = Path(*ruta.parts[1:])

    ruta_final = (base_dir / ruta).resolve()

    # Verificar que la ruta final esté dentro del directorio base
    if not str(ruta_final).startswith(str(base_dir)):
        raise PermissionError(
            f"Path traversal detectado. Ruta '{ruta_str}' está fuera del workspace."
        )

    return ruta_final
