"""Isolate plugin imports, editable mappings and workers from the host and test checkout."""

from __future__ import annotations
import shutil
import sys
import types
from enum import Enum
from pathlib import Path
from pydantic import BaseModel, Field


def stage_plugin(source: Path, destination: Path) -> None:
    files = [*source.glob("*.py"), source / "_manifest.json"]
    for package in (
        "companion",
        "core",
        "emotion",
        "integrations",
        "linkage",
        "planning",
        "qzone",
        "tools",
        "video_watch",
    ):
        files.extend((source / package).rglob("*.py"))
    for path in files:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copytree(source / "mappings" / "default_mappings", destination / "mappings" / "default_mappings")
    sys.path.insert(0, str(destination))
    sys.dont_write_bytecode = True


def install_sdk() -> None:
    sdk = types.ModuleType("maibot_sdk")
    sdk_types = types.ModuleType("maibot_sdk.types")

    class Mode(Enum):
        BLOCKING = "blocking"
        OBSERVE = "observe"
        EARLY = "early"
        NORMAL = "normal"
        LATE = "late"
        SKIP = "skip"
        ABORT = "abort"
        LOG = "log"

    class Plugin:
        def get_components(self):
            result = []
            for name in dir(type(self)):
                method = getattr(type(self), name, None)
                info = getattr(method, "component_declaration", None)
                if isinstance(info, dict):
                    result.append(dict(info))
            return result

    def decorator(kind):
        def factory(name="", **metadata):
            def decorate(fn):
                fn.component_declaration = {"name": name, "type": kind, "metadata": dict(metadata)}
                return fn

            return decorate

        return factory

    def hook(hook_name, name="", **metadata):
        return decorator("hook_handler")(name, hook=hook_name, **metadata)

    sdk.MaiBotPlugin = Plugin
    sdk.PluginConfigBase = BaseModel
    sdk.Field = Field
    sdk.API = decorator("api")
    sdk.Command = decorator("command")
    sdk.Tool = decorator("tool")
    sdk.HookHandler = hook
    for key in ("ErrorPolicy", "HookMode", "HookOrder"):
        setattr(sdk_types, key, Mode)
    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = sdk_types
