"""
model_manager.py — Carga y descarga dinámica de modelos LLM.

CRÍTICO para hardware con 16 GB RAM:
  - Nunca se tienen 2 modelos cargados a la vez.
  - load_model() descarga el modelo actual antes de cargar el nuevo.
  - Comunicación vía HTTP con un servidor compatible con la API de OpenAI
    (ej: llama.cpp --server, LM Studio, Ollama, vLLM).
"""

import time
import logging
import requests
from typing import Optional

logger = logging.getLogger("agent.model_manager")


# ──────────────────────────────────────────────
#  Configuración de modelos disponibles
# ──────────────────────────────────────────────

MODEL_REGISTRY: dict[str, dict] = {
    "agent": {
        "name": "agent",
        "description": "Modelo liviano para interpretar, planificar y validar.",
        # El model_id es lo que se envía al endpoint /v1/chat/completions.
        # Para llama.cpp --server, el modelo ya está cargado en el servidor;
        # para Ollama, es el nombre del modelo (ej: "mistral").
        "model_id": "mistral-7b-instruct",
        "base_url": "http://localhost:8080",   # llama.cpp server
        "api_key": "not-needed",               # algunos servidores lo ignoran
        "timeout": 60,
        "max_tokens": 512,
        "temperature": 0.2,
    },
    "code": {
        "name": "code",
        "description": "Modelo especializado en generación de código y acciones.",
        "model_id": "deepseek-coder-6.7b-instruct",
        "base_url": "http://localhost:8081",   # segundo puerto para el modelo code
        "api_key": "not-needed",
        "timeout": 120,
        "max_tokens": 1024,
        "temperature": 0.1,
    },
}


class ModelManager:
    """
    Gestiona qué modelo está activo y provee inferencia HTTP.

    Patrón de uso:
        mm = ModelManager()
        mm.load_model("agent")
        respuesta = mm.infer([{"role": "user", "content": "hola"}])
        mm.unload_model()
    """

    def __init__(self, debug: bool = False):
        self._active_name: Optional[str] = None
        self._active_config: Optional[dict] = None
        self.debug = debug
        logger.debug("ModelManager inicializado.")

    # ──────────────────────────────────────────
    #  Carga / descarga
    # ──────────────────────────────────────────

    def load_model(self, name: str) -> None:
        """
        Activa un modelo por nombre.

        Si ya hay uno cargado, se descarga primero (liberación de memoria lógica).
        Para servidores tipo llama.cpp con un solo modelo por proceso, este método
        simplemente apunta al endpoint correcto. Para servidores dinámicos (Ollama,
        vLLM), aquí se haría la llamada al endpoint de carga.

        Args:
            name: Clave en MODEL_REGISTRY ("agent" | "code").
        """
        if name not in MODEL_REGISTRY:
            raise ValueError(f"Modelo '{name}' no registrado. Disponibles: {list(MODEL_REGISTRY)}")

        if self._active_name == name:
            logger.debug("Modelo '%s' ya está activo.", name)
            return

        if self._active_name is not None:
            self.unload_model()

        config = MODEL_REGISTRY[name]
        self._active_name = name
        self._active_config = config

        # ── Hook para servidores con carga dinámica (Ollama, etc.) ──
        # Si tu servidor soporta pre-carga explícita, implementa aquí.
        self._try_warmup()

        logger.info("▶ Modelo cargado: '%s' → %s", name, config["base_url"])

    def unload_model(self) -> None:
        """
        Desactiva el modelo actual.

        Para servidores con descarga dinámica, extender este método.
        """
        if self._active_name is None:
            return
        logger.info("◀ Modelo descargado: '%s'", self._active_name)
        self._active_name = None
        self._active_config = None

    def get_active_model(self) -> Optional[str]:
        return self._active_name

    # ──────────────────────────────────────────
    #  Inferencia
    # ──────────────────────────────────────────

    def infer(
        self,
        messages: list[dict],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """
        Envía una solicitud de inferencia al modelo activo.

        Args:
            messages: Lista de mensajes en formato OpenAI
                      [{"role": "user"|"assistant"|"system", "content": "..."}]
            max_tokens: Override del límite de tokens de salida.
            temperature: Override de temperatura.
            system_prompt: Si se provee, se inserta como primer mensaje "system".

        Returns:
            Texto generado por el modelo.

        Raises:
            RuntimeError: Si no hay modelo cargado o falla la solicitud.
        """
        if self._active_config is None:
            raise RuntimeError("No hay modelo cargado. Llama load_model() primero.")

        cfg = self._active_config

        # Preparar mensajes (insertar system prompt si se provee)
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        payload = {
            "model": cfg["model_id"],
            "messages": full_messages,
            "max_tokens": max_tokens or cfg["max_tokens"],
            "temperature": temperature if temperature is not None else cfg["temperature"],
            "stream": False,
        }

        endpoint = f"{cfg['base_url']}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }

        if self.debug:
            logger.debug("→ POST %s | tokens_max=%s | msgs=%d",
                         endpoint, payload["max_tokens"], len(full_messages))

        t0 = time.time()
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=cfg["timeout"],
            )
            resp.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"No se pudo conectar al servidor del modelo '{self._active_name}' "
                f"en {cfg['base_url']}. ¿Está corriendo el servidor? Error: {e}"
            ) from e
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"Timeout ({cfg['timeout']}s) esperando respuesta del modelo '{self._active_name}'."
            )
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Error HTTP del modelo: {e}\nRespuesta: {resp.text[:500]}") from e

        elapsed = time.time() - t0
        data = resp.json()

        try:
            content = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Respuesta inesperada del modelo: {data}") from e

        if self.debug:
            logger.debug("← Respuesta en %.2fs | chars=%d", elapsed, len(content))
            logger.debug("Contenido:\n%s", content[:300])

        return content

    # ──────────────────────────────────────────
    #  Helpers internos
    # ──────────────────────────────────────────

    def _try_warmup(self) -> None:
        """
        Intenta un ping liviano al servidor para verificar conectividad.
        No es bloqueante: solo loguea si hay problemas.
        """
        if self._active_config is None:
            return
        try:
            resp = requests.get(
                f"{self._active_config['base_url']}/health",
                timeout=3,
            )
            if resp.status_code == 200:
                logger.debug("Servidor '%s' responde OK.", self._active_name)
        except Exception:
            logger.warning(
                "No se pudo hacer ping al servidor '%s' en %s. "
                "Asegúrate de que esté corriendo antes de ejecutar el agente.",
                self._active_name,
                self._active_config["base_url"],
            )

    # ──────────────────────────────────────────
    #  Context manager (uso opcional)
    # ──────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.unload_model()
