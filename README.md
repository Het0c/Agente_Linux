# Agente Linux — Agente LLM Híbrido con Capabilities

Sistema de agente local basado en LLMs, diseñado para equipos con recursos limitados y para mantener una separación clara entre razonamiento, ejecución, memoria y aprendizaje continuo.

El flujo principal está controlado por Python: el LLM interpreta, planifica, genera acciones estructuradas y valida resultados, pero no controla el loop de ejecución.

## Características principales

- **Arquitectura desacoplada**: `main.py` orquesta, `agent.py` razona, `executor.py` transforma pasos en acciones y `capabilities/` ejecuta operaciones concretas.
- **Dos modelos locales especializados**:
  - modelo `agent` para interpretar, planificar, validar y replanificar;
  - modelo `code` para producir acciones JSON ejecutables.
- **Sistema de capabilities**: las acciones ya no viven acopladas al ejecutor; se registran y despachan mediante `CapabilityManager`.
- **Bus de eventos**: `EventBus` desacopla emisores y observadores para registrar éxitos, errores y patrones.
- **Aprendizaje pasivo**: `LearningCapability` observa eventos, clasifica errores y persiste aprendizajes en JSONL.
- **Reflexión y propuestas de mejora**: `ReflectionCapability` analiza aprendizajes acumulados y `SkillBuilder` propone nuevas capabilities o mejoras.
- **Workspace controlado**: las operaciones de archivo se contienen dentro de `agent_workspace/` para reducir riesgos de escritura fuera del área del agente.
- **Persistencia opcional**: `AgentMemory` puede guardar el estado de una tarea en disco.

## Arquitectura del proyecto

```text
main.py                         Orquestador y CLI
agent.py                        Intérprete, planificador, validador y replanificador
executor.py                     Genera acciones JSON vía modelo code y las delega
model_manager.py                Cliente HTTP para modelos compatibles con OpenAI API
memory.py                       Estado persistente de la tarea
utils.py                        Prompts, parser JSON, heurísticas y salida por consola
config_agents.json              Configuración local para servidores llama.cpp
start_agents.sh                 Arranque de servidores logic_agent y coder_agent
requirements.txt                Dependencias Python mínimas

capabilities/
├── base_capability.py          Contrato base para capabilities ejecutoras
├── capability_manager.py       Router de acciones a capabilities
├── event_bus.py                Bus pub/sub síncrono
├── filesystem_capability.py    Acciones de archivos y directorios
├── python_capability.py        Ejecución controlada de Python y cálculos
├── general_capability.py       Respuestas directas e introspección
├── learning_capability.py      Observador de eventos y memoria de aprendizajes
├── reflection_capability.py    Análisis periódico de aprendizajes
└── skill_builder.py            Propuestas de nuevas skills/capabilities
```

## Flujo de ejecución

```text
Usuario
  ↓
[Agent.interpretar]
  ↓
¿Tarea simple?
  ├─ Sí → [Executor] → [CapabilityManager] → [Capability]
  └─ No
      ↓
    [Agent.planificar]
      ↓
    Loop por paso:
      1. [Executor] pide al modelo code una acción JSON
      2. [CapabilityManager] despacha la acción a la capability adecuada
      3. La capability ejecuta y emite eventos al EventBus
      4. [LearningCapability] observa y guarda aprendizajes
      5. [Agent.validar] decide continuar, repetir o replanificar
      6. Si falla repetidamente, [Agent.replanificar] ajusta el plan
```

## Requisitos

- Python 3.10+
- `pip`
- `jq` si usas `start_agents.sh`
- Servidor LLM local compatible con `/v1/chat/completions`, por ejemplo:
  - llama.cpp server
  - LM Studio
  - Ollama con endpoint compatible
  - vLLM

Instala dependencias Python:

```bash
pip install -r requirements.txt
```

## Configuración de modelos

La inferencia se configura en `model_manager.py`, dentro de `MODEL_REGISTRY`:

```python
MODEL_REGISTRY = {
    "agent": {
        "model_id": "mistral-7b-instruct",
        "base_url": "http://localhost:8080",
        "max_tokens": 512,
        "temperature": 0.2,
    },
    "code": {
        "model_id": "deepseek-coder-6.7b-instruct",
        "base_url": "http://localhost:8081",
        "max_tokens": 1024,
        "temperature": 0.1,
    },
}
```

Para entornos llama.cpp, también puedes ajustar `config_agents.json` y usar el script de arranque:

```bash
chmod +x start_agents.sh
./start_agents.sh
```

El archivo `config_agents.json` define rutas de modelos, puertos, contexto, threads, batch size, backend y directorio de logs para `logic_agent` y `coder_agent`.

## Uso

```bash
# Tarea directa
python main.py "Crea un archivo Python con los primeros 20 números de Fibonacci"

# Modo debug con logging detallado
python main.py --debug "Analiza y resume el archivo datos.csv"

# Persistir estado de la tarea en disco
python main.py --persist estado_agente.json "Genera un informe de ventas"

# Modo interactivo
python main.py --interactive
```

Los logs se escriben en `agent.log` y en stdout. En modo `--debug` se añaden detalles de inferencia y payloads útiles para diagnóstico.

## Acciones disponibles mediante capabilities

Las acciones reales se registran dinámicamente en `CapabilityManager` y se inyectan en el prompt del ejecutor.

### FilesystemCapability

- `crear_archivo`
- `leer_archivo`
- `escribir_archivo`
- `append_archivo`
- `crear_directorio`

Todas las rutas se normalizan dentro de `agent_workspace/`; se rechazan rutas vacías y traversal como `../../`.

### PythonCapability

- `ejecutar_python`
- `calcular`

La ejecución de Python usa un sandbox básico con builtins permitidos y bloqueos para operaciones de sistema, red e I/O directo.

### GeneralCapability

- `responder`
- `listar_acciones`

Permite respuestas directas al usuario e introspección de acciones registradas.

## Memoria y aprendizaje

El proyecto mantiene dos niveles de memoria:

1. **Memoria de tarea (`AgentMemory`)**: objetivo, plan, paso actual, historial, resultados, intentos y estado global.
2. **Memoria de capabilities/aprendizaje**:
   - memorias por capability en `workspace/capabilities/<capability>/memory.jsonl`;
   - aprendizajes globales en `workspace/memory/learnings/*.jsonl`;
   - patrones pendientes en `workspace/memory/pending/`;
   - propuestas de skills en `workspace/skills/proposed/`.

Ejemplo simplificado de estado de tarea:

```json
{
  "objetivo": "tarea original del usuario",
  "plan": ["Paso 1", "Paso 2"],
  "paso_actual": 1,
  "historial": [],
  "resultados": ["resultado paso 1", ""],
  "intentos_paso": 0,
  "estado_global": "running",
  "timestamp_inicio": 1234567880,
  "timestamp_fin": null
}
```

## Añadir una nueva capability

1. Crea una clase que herede de `BaseCapability`.
2. Define `supported_actions`.
3. Implementa `execute(action, params)` y sus handlers internos.
4. Emite eventos con `self.emit(...)` cuando haya éxitos, errores o señales útiles.
5. Registra la capability en el punto de inicialización del sistema para que `CapabilityManager` pueda despacharla.

Ejemplo mínimo:

```python
from capabilities.base_capability import BaseCapability

class MiCapability(BaseCapability):
    @property
    def supported_actions(self) -> list[str]:
        return ["mi_accion"]

    def execute(self, action: str, params: dict) -> tuple[bool, str]:
        if action != "mi_accion":
            return False, f"ERROR: acción no soportada: {action}"
        valor = params.get("valor", "")
        self.emit("success", "mi_accion_ok", action=action, context=params, result=str(valor))
        return True, f"Resultado: {valor}"
```

## Consideraciones para hardware limitado

- El sistema está pensado para modelos cuantizados y servidores locales.
- `ModelManager` mantiene un único modelo activo a nivel lógico y descarga el anterior antes de cambiar de rol.
- Para llama.cpp con dos procesos, usa puertos separados: `8080` para el agente lógico y `8081` para el agente de código.
- Los resultados se recortan antes de volver al validador para no saturar el contexto.
- Ajusta `ctx_size`, `threads`, `batch_size` y `n_gpu_layers` en `config_agents.json` según tu hardware.

## Notas de desarrollo

- No edites directamente el prompt del ejecutor para enumerar acciones fijas si usas capabilities: `Executor` construye una lista dinámica desde `CapabilityManager`.
- `LearningCapability`, `ReflectionCapability` y `SkillBuilder` no deberían ejecutar acciones del usuario directamente; funcionan como observadores, analizadores y generadores de propuestas.
- Revisa periódicamente los JSONL de `workspace/memory/` para detectar errores recurrentes y promover mejoras.
