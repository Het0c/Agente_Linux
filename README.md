# Agente LLM Híbrido — Arquitectura Desacoplada

Sistema de agente basado en LLMs diseñado para hardware limitado (16 GB RAM),
con separación clara entre planificación, ejecución y validación.

## Arquitectura

```
main.py              → Orquestador y loop de control (Python puro, no LLM)
├── agent.py         → Intérprete, Planificador, Validador (modelo "agent")
├── executor.py      → Ejecutor de acciones estructuradas (modelo "code")
├── model_manager.py → Carga/descarga dinámica de modelos (nunca 2 a la vez)
├── memory.py        → Estado externo persistente (JSON en RAM + disco)
└── utils.py         → Prompts, parsers JSON, detección de tareas simples
```

### Flujo de ejecución

```
Usuario → [Intérprete] → ¿Simple?
                              ↓ Sí → [Ejecutor] → Resultado
                              ↓ No
                         [Planificador] → Plan de pasos
                              ↓
                         LOOP por cada paso:
                           [Ejecutor (modelo code)] → Acción estructurada → Python ejecuta
                           [Validador (modelo agent)] → ¿OK?
                             ├── continuar → siguiente paso
                             ├── repetir   → reintento (máx 3)
                             └── replanificar → nuevo plan desde ese punto
```

## Requisitos

- Python 3.10+
- Servidor LLM local compatible con API OpenAI (llama.cpp, Ollama, LM Studio, vLLM)

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración de modelos

Edita `model_manager.py`, sección `MODEL_REGISTRY`:

```python
MODEL_REGISTRY = {
    "agent": {
        "model_id": "mistral-7b-instruct",   # nombre del modelo en tu servidor
        "base_url": "http://localhost:8080",   # URL de tu servidor llama.cpp
        ...
    },
    "code": {
        "model_id": "deepseek-coder-6.7b-instruct",
        "base_url": "http://localhost:8081",   # segundo servidor para modelo code
        ...
    },
}
```

### Opción A: llama.cpp (recomendado para 16 GB RAM)

```bash
# Terminal 1 — modelo agente
./llama-server -m mistral-7b-instruct.Q4_K_M.gguf --port 8080 --ctx-size 4096

# Terminal 2 — modelo code (cargar solo cuando el agente lo necesite)
./llama-server -m deepseek-coder-6.7b.Q4_K_M.gguf --port 8081 --ctx-size 4096
```

### Opción B: Ollama

```bash
ollama serve
# Cambia base_url a http://localhost:11434 en MODEL_REGISTRY
```

## Uso

```bash
# Tarea directa
python main.py "Crea un archivo Python con los primeros 20 números de Fibonacci"

# Modo debug (logging detallado)
python main.py --debug "Analiza y resume el archivo datos.csv"

# Con persistencia de estado en disco
python main.py --persist estado_agente.json "Genera un informe de ventas"

# Modo interactivo
python main.py --interactive
```

## Añadir nuevas acciones (Skills)

Para extender el ejecutor con nuevas capacidades, edita `executor.py`:

```python
@register_action("mi_nueva_accion")
def handle_mi_nueva_accion(params: dict) -> str:
    # params viene del JSON que genera el modelo code
    valor = params.get("mi_parametro", "")
    # ... lógica Python aquí
    return "resultado como string"
```

Y actualiza `EXECUTOR_PROMPT` en `utils.py` para que el modelo sepa que
la acción existe.

## Estructura de estado (AgentMemory)

```json
{
  "objetivo": "tarea original del usuario",
  "plan": ["Paso 1: ...", "Paso 2: ...", "..."],
  "paso_actual": 2,
  "historial": [
    {"timestamp": 1234567890, "tipo": "interpretacion", "detalle": "..."},
    {"timestamp": 1234567891, "tipo": "ejecucion",      "detalle": "..."}
  ],
  "resultados": ["resultado paso 1", "resultado paso 2", ""],
  "intentos_paso": 0,
  "estado_global": "running",
  "timestamp_inicio": 1234567880,
  "timestamp_fin": null
}
```

## Consideraciones de memoria (16 GB RAM)

- Los modelos NUNCA se cargan simultáneamente.
- El modelo "agent" se carga → infiere → descarga antes de cada operación.
- El modelo "code" se carga → infiere → descarga en cada paso.
- El contexto pasado al LLM está limitado a lo esencial (no se arrastra historial completo).
- Los resultados de cada paso se truncan a 1000 chars para el validador.

## Logs

El agente genera logs en `agent.log` (archivo) y stdout.
En modo `--debug`, se incluyen los payloads completos enviados al LLM.

## Observabilidad y Telemetría (nuevo)

Se añadió un subsistema de observabilidad productivo:

- `telemetry.py`: bus de eventos, persistencia SQLite y métricas LLM/agent.
- `observability_api.py`: API FastAPI + WebSocket para dashboard live.

### Métricas LLM registradas

- modelo, endpoint, latencia total
- prompt eval time / generation time
- prompt/completion tokens
- tokens/s
- % de contexto usado (`ctx_size`)
- retries
- GPU/VRAM/temperatura (si NVML está disponible)

### Ejecutar API de observabilidad

```bash
uvicorn observability_api:app --host 0.0.0.0 --port 8090
```

Endpoints:

- `GET /healthz`
- `GET /snapshot`
- `WS /ws/live`

### Base de datos

Se crea `telemetry.db` con tablas:

- `llm_metrics`
- `agent_events`
