"""
capabilities/ — Paquete de capabilities del agente.

Cada capability encapsula un dominio de ejecución:
  - FilesystemCapability  → operaciones de archivos
  - PythonCapability      → ejecución de código Python
  - GeneralCapability     → acciones de utilidad general

Componentes de aprendizaje (no participan en el flujo de ejecución):
  - LearningCapability    → observa todos los eventos, clasifica, persiste
  - ReflectionCapability  → analiza aprendizajes periódicamente
  - SkillBuilder          → convierte patrones en propuestas de nuevas capabilities

Infraestructura:
  - EventBus              → bus de eventos pub/sub para desacoplar observadores
  - BaseCapability        → clase base abstracta
  - CapabilityManager     → router central de acciones a capabilities
"""

from capabilities.event_bus import EventBus
from capabilities.base_capability import BaseCapability
from capabilities.capability_manager import CapabilityManager
from capabilities.learning_capability import LearningCapability
from capabilities.filesystem_capability import FilesystemCapability
from capabilities.python_capability import PythonCapability
from capabilities.general_capability import GeneralCapability
from capabilities.reflection_capability import ReflectionCapability
from capabilities.skill_builder import SkillBuilder

__all__ = [
    "EventBus",
    "BaseCapability",
    "CapabilityManager",
    "LearningCapability",
    "FilesystemCapability",
    "PythonCapability",
    "GeneralCapability",
    "ReflectionCapability",
    "SkillBuilder",
]
