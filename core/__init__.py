"""属于麦麦的生活 — 公共基础设施（跨模块共享；各业务模块在 planning/ companion/ qzone/）。"""

from .diag import DecisionLogger, LLMCallLogger
from .storage import load_json, save_json_atomic

__all__ = [
    "DecisionLogger",
    "LLMCallLogger",
    "LLMService",
    "load_json",
    "save_json_atomic",
]


def __getattr__(name):
    # Keep transport/leaf imports from eagerly loading the facade through this package.
    if name == "LLMService":
        from .llm_utils import LLMService
        globals()[name] = LLMService
        return LLMService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
