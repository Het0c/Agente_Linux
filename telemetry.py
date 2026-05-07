"""Infra de observabilidad para agente LLM distribuido (Linux-first)."""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import psutil

try:
    import pynvml  # type: ignore
    NVML_AVAILABLE = True
except Exception:  # pragma: no cover
    NVML_AVAILABLE = False

logger = logging.getLogger("agent.telemetry")


@dataclass
class LLMRequestMetric:
    task_id: str
    request_id: str
    phase: str
    model_name: str
    endpoint: str
    total_ms: float
    prompt_eval_ms: float
    generation_ms: float
    prompt_tokens: int
    completion_tokens: int
    tokens_per_sec: float
    context_used_pct: float
    retries: int
    kv_cache_mb: Optional[float]
    gpu_id: Optional[int]
    vram_used_mb: Optional[float]
    gpu_temp_c: Optional[float]
    batch_size: Optional[int]
    ctx_size: Optional[int]
    timestamp: float = field(default_factory=time.time)


@dataclass
class AgentEvent:
    task_id: str
    event_type: str
    severity: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.time)


class TelemetryStore:
    def __init__(self, db_path: str = "telemetry.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_metrics (
                  request_id TEXT PRIMARY KEY,
                  task_id TEXT, phase TEXT, model_name TEXT, endpoint TEXT,
                  total_ms REAL, prompt_eval_ms REAL, generation_ms REAL,
                  prompt_tokens INTEGER, completion_tokens INTEGER,
                  tokens_per_sec REAL, context_used_pct REAL, retries INTEGER,
                  kv_cache_mb REAL, gpu_id INTEGER, vram_used_mb REAL,
                  gpu_temp_c REAL, batch_size INTEGER, ctx_size INTEGER, timestamp REAL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task_id TEXT, event_type TEXT, severity TEXT,
                  payload_json TEXT, timestamp REAL
                )
                """
            )

    def save_llm_metric(self, m: LLMRequestMetric) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO llm_metrics VALUES
                (:request_id,:task_id,:phase,:model_name,:endpoint,
                 :total_ms,:prompt_eval_ms,:generation_ms,
                 :prompt_tokens,:completion_tokens,:tokens_per_sec,:context_used_pct,:retries,
                 :kv_cache_mb,:gpu_id,:vram_used_mb,:gpu_temp_c,:batch_size,:ctx_size,:timestamp)
                """,
                asdict(m),
            )

    def save_event(self, e: AgentEvent) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO agent_events(task_id,event_type,severity,payload_json,timestamp) VALUES(?,?,?,?,?)",
                (e.task_id, e.event_type, e.severity, json.dumps(e.payload, ensure_ascii=False), e.timestamp),
            )


class ResourceSampler:
    def __init__(self):
        self._nvml_ok = False
        if NVML_AVAILABLE:
            try:
                pynvml.nvmlInit()
                self._nvml_ok = True
            except Exception:
                self._nvml_ok = False

    def sample(self) -> dict[str, Any]:
        sample = {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "ram_used_mb": psutil.virtual_memory().used / (1024 * 1024),
            "gpus": [],
        }
        if self._nvml_ok:
            count = pynvml.nvmlDeviceGetCount()
            for i in range(count):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                sample["gpus"].append({"gpu_id": i, "vram_used_mb": mem.used / (1024 * 1024), "temp_c": float(temp)})
        return sample


class TelemetryBus:
    """Bus asíncrono de eventos + persistencia + exportación para dashboard."""

    def __init__(self, db_path: str = "telemetry.db"):
        self.store = TelemetryStore(db_path=db_path)
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.latest: dict[str, Any] = {}
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "llm":
                self.store.save_llm_metric(payload)
                self.latest[f"llm:{payload.request_id}"] = asdict(payload)
            elif kind == "event":
                self.store.save_event(payload)
                self.latest[f"event:{payload.task_id}:{payload.event_type}"] = asdict(payload)

    def emit_llm(self, metric: LLMRequestMetric) -> None:
        self.queue.put(("llm", metric))

    def emit_event(self, event: AgentEvent) -> None:
        self.queue.put(("event", event))

    def snapshot(self) -> dict[str, Any]:
        return {"latest": self.latest, "queue_size": self.queue.qsize()}

    def shutdown(self) -> None:
        self._stop.set()
        self._worker.join(timeout=1.0)


def build_llm_metric_from_response(
    *,
    task_id: str,
    phase: str,
    model_name: str,
    endpoint: str,
    elapsed_s: float,
    response_json: dict[str, Any],
    retries: int,
    ctx_size: Optional[int],
    sampler: Optional[ResourceSampler] = None,
) -> LLMRequestMetric:
    usage = response_json.get("usage", {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = max(1, completion_tokens)
    tokens_per_sec = completion_tokens / max(elapsed_s, 1e-6)

    timing = response_json.get("timings", {})
    prompt_eval_ms = float(timing.get("prompt_eval_ms", 0.0))
    generation_ms = float(timing.get("generation_ms", elapsed_s * 1000.0))

    gpu_id = None
    vram = None
    temp = None
    if sampler:
        r = sampler.sample()
        if r.get("gpus"):
            g0 = r["gpus"][0]
            gpu_id, vram, temp = g0.get("gpu_id"), g0.get("vram_used_mb"), g0.get("temp_c")

    return LLMRequestMetric(
        task_id=task_id,
        request_id=str(uuid.uuid4()),
        phase=phase,
        model_name=model_name,
        endpoint=endpoint,
        total_ms=elapsed_s * 1000.0,
        prompt_eval_ms=prompt_eval_ms,
        generation_ms=generation_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        tokens_per_sec=tokens_per_sec,
        context_used_pct=(prompt_tokens / ctx_size * 100.0) if ctx_size else 0.0,
        retries=retries,
        kv_cache_mb=None,
        gpu_id=gpu_id,
        vram_used_mb=vram,
        gpu_temp_c=temp,
        batch_size=None,
        ctx_size=ctx_size,
    )
