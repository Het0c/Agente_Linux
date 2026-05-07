"""
utils.py — Utilidades compartidas: prompts, parsers y detección de tareas simples.

Todos los prompts del sistema están AQUÍ, claramente separados por rol.
Modificar los prompts es la palanca principal para ajustar comportamiento.
"""

import json
import re
import logging
from typing import Optional

logger = logging.getLogger("agent.utils")


# ══════════════════════════════════════════════
#  PROMPTS DEL SISTEMA (separados por rol)
# ══════════════════════════════════════════════

# ── Intérprete ──────────────────────────────
INTERPRETER_PROMPT = """Eres un intérprete de tareas para un agente de IA.

Tu única función es analizar la solicitud del usuario y extraer:
1. El objetivo claro y concreto de la tarea.
2. Las restricciones o condiciones importantes.
3. El tipo de tarea (simple | compleja).

Una tarea es SIMPLE si:
- Requiere una sola acción (crear un archivo, responder una pregunta, calcular algo).
- No tiene pasos dependientes entre sí.
- Puede completarse en una sola llamada al modelo de código.

Una tarea es COMPLEJA si:
- Requiere múltiples pasos encadenados.
- Tiene dependencias entre pasos.
- Involucra lógica condicional o iterativa.

Responde SIEMPRE en JSON con este formato exacto:
{
  "objetivo": "descripción clara y concreta del objetivo",
  "restricciones": ["restricción 1", "restricción 2"],
  "tipo": "simple" | "compleja",
  "razon": "por qué es simple o compleja"
}

No incluyas texto fuera del JSON."""

# ── Planificador ─────────────────────────────
PLANNER_PROMPT = """Eres un planificador de tareas para un agente de IA.

Tu función es descomponer un objetivo complejo en una lista ORDENADA de pasos simples y atómicos.

Reglas estrictas:
- Cada paso debe ser una acción CONCRETA y VERIFICABLE.
- Los pasos deben ser independientes en la medida de lo posible.
- NO incluyas código en los pasos, solo descripciones de acciones.
- Máximo 8 pasos. Si necesitas más, algo está mal en la descomposición.
- Cada paso debe comenzar con un verbo de acción (Crear, Leer, Escribir, Calcular, Verificar...).

Responde SIEMPRE en JSON con este formato exacto:
{
  "pasos": [
    "Paso 1: descripción concreta",
    "Paso 2: descripción concreta",
    ...
  ],
  "razon": "explicación breve de la estrategia de descomposición"
}

No incluyas texto fuera del JSON."""

# ── Ejecutor (modelo code) ───────────────────
EXECUTOR_PROMPT = """Eres un agente ejecutor especializado en generar acciones estructuradas.

Tu función es recibir un paso concreto de un plan y generar la acción necesaria para ejecutarlo.

IMPORTANTE: No ejecutas código directamente. Generas una descripción estructurada de la acción
que el sistema Python ejecutará de forma segura.

Tipos de acciones disponibles:
- crear_archivo: Crear un nuevo archivo con contenido.
- leer_archivo: Leer el contenido de un archivo existente.
- escribir_archivo: Sobrescribir un archivo existente.
- append_archivo: Agregar contenido al final de un archivo.
- ejecutar_python: Ejecutar un fragmento de código Python puro (sin I/O peligroso).
- calcular: Realizar un cálculo matemático o de texto.
- responder: Devolver una respuesta directa al usuario (sin acción en sistema de archivos).
- crear_directorio: Crear un directorio.

Responde SIEMPRE en JSON con este formato exacto:
{
  "accion": "nombre_de_accion",
  "parametros": {
    // parámetros específicos de la acción
  },
  "razon": "por qué esta acción cumple el paso"
}

Ejemplos:
- Para crear_archivo: {"ruta": "output/resultado.txt", "contenido": "..."}
- Para ejecutar_python: {"codigo": "resultado = 2 + 2", "variable_retorno": "resultado"}
- Para responder: {"mensaje": "La respuesta es..."}
- Para calcular: {"expresion": "suma de los números pares del 1 al 100", "resultado": "2550"}

No incluyas texto fuera del JSON."""

# ── Validador ────────────────────────────────
VALIDATOR_PROMPT = """Eres un validador de resultados para un agente de IA.

Tu función es evaluar si la ejecución de un paso fue exitosa y si el resultado
es correcto y coherente con el objetivo.

Criterios de evaluación:
1. El resultado existe (no está vacío ni es un error).
2. El resultado es coherente con el paso ejecutado.
3. El resultado avanza hacia el objetivo general.
4. No hay errores explícitos en el resultado.

Decisiones posibles:
- "continuar": El paso fue exitoso, avanzar al siguiente.
- "repetir": El paso falló o el resultado es incorrecto, repetir el mismo paso.
- "replanificar": El paso falló de forma irrecuperable o el plan ya no es válido.

Responde SIEMPRE en JSON con este formato exacto:
{
  "decision": "continuar" | "repetir" | "replanificar",
  "exitoso": true | false,
  "razon": "explicación de la decisión",
  "sugerencia": "qué cambiar si se va a repetir o replanificar (puede ser vacío)"
}

No incluyas texto fuera del JSON."""


# ══════════════════════════════════════════════
#  Detección de tareas simples
# ══════════════════════════════════════════════

# Patrones que sugieren tareas simples (bypass del planificador)
_SIMPLE_PATTERNS = [
    r"^(qué es|qué son|define|explica brevemente|dime qué)\s",
    r"^(cuánto es|calcula|suma|resta|multiplica|divide)\s",
    r"^(crea un archivo|crea el archivo|genera el archivo)\s.{0,60}$",
    r"^(hola|hi|hello|buenos días|buenas tardes)",
    r"^(convierte|transforma)\s.{0,40}\s(a|en)\s.{0,40}$",
    r"^(lista|listar|muestra)\s.{0,40}$",
]

_SIMPLE_KEYWORDS = {
    "qué hora", "fecha actual", "versión", "ayuda", "help",
    "ping", "test", "hola", "hello",
}

_COMPLEX_KEYWORDS = {
    "sistema", "aplicación", "app", "proyecto", "módulo",
    "arquitectura", "integra", "automatiza", "pipeline",
    "múltiples archivos", "varios pasos", "flujo completo",
    "api", "servidor", "base de datos",
}


def is_simple_task(user_input: str) -> bool:
    """
    Determina si una tarea es suficientemente simple como para saltarse el planificador.

    Lógica:
    1. Si contiene keywords de complejidad → compleja.
    2. Si la entrada es muy corta y coincide con patrones simples → simple.
    3. Si el LLM lo ha clasificado como simple en la interpretación → usar esa clasificación.

    Args:
        user_input: Texto original del usuario.

    Returns:
        True si la tarea es simple (bypass del planificador).
    """
    text = user_input.strip().lower()

    # Complejidad explícita → siempre planificar
    for kw in _COMPLEX_KEYWORDS:
        if kw in text:
            logger.debug("Tarea marcada como COMPLEJA por keyword: '%s'", kw)
            return False

    # Muy corta → probablemente simple
    if len(text) < 20:
        logger.debug("Tarea marcada como SIMPLE por longitud corta.")
        return True

    # Keywords de tareas triviales
    for kw in _SIMPLE_KEYWORDS:
        if kw in text:
            logger.debug("Tarea marcada como SIMPLE por keyword: '%s'", kw)
            return True

    # Patrones regex
    for pattern in _SIMPLE_PATTERNS:
        if re.match(pattern, text, re.IGNORECASE):
            logger.debug("Tarea marcada como SIMPLE por patrón regex.")
            return True

    return False


# ══════════════════════════════════════════════
#  Parsers JSON robustos
# ══════════════════════════════════════════════

def parse_json_response(text: str, context: str = "") -> Optional[dict]:
    """
    Parsea la respuesta del LLM como JSON de forma robusta.

    Intenta:
    1. Parseo directo.
    2. Extracción de bloque ```json ... ```.
    3. Búsqueda del primer { ... } válido.

    Args:
        text: Texto crudo devuelto por el modelo.
        context: Descripción del contexto para el logging de errores.

    Returns:
        Diccionario parseado, o None si falló todo.
    """
    text = text.strip()

    # Intento 1: parseo directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Intento 2: bloque markdown ```json ... ```
    md_match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if md_match:
        try:
            return json.loads(md_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Intento 3: primer { ... } balanceado
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    logger.warning("No se pudo parsear JSON [%s]. Texto: %s", context, text[:200])
    return None


# ══════════════════════════════════════════════
#  Formateo de salida para consola
# ══════════════════════════════════════════════

def print_banner(title: str, char: str = "═") -> None:
    width = 60
    print(f"\n{char * width}")
    print(f"  {title}")
    print(f"{char * width}")


def print_step(numero: int, total: int, descripcion: str) -> None:
    print(f"\n  [{numero}/{total}] {descripcion}")


def print_status(label: str, valor: str, emoji: str = "→") -> None:
    print(f"  {emoji} {label}: {valor}")


def print_result(resultado: str, exitoso: bool) -> None:
    icono = "✅" if exitoso else "❌"
    print(f"\n  {icono} Resultado: {resultado[:300]}")


def print_section(titulo: str) -> None:
    print(f"\n  ── {titulo} {'─' * (40 - len(titulo))}")
