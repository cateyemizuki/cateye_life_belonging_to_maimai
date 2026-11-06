"""短任务的有界提示约束。只观察响应，不重试、不改模型、超时或 Token。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

DIRECT_OUTPUT_HINT = "请直接输出最终结果，禁止输出思考过程或reasoning字段。"
SHORT_EVENTS = frozenset({"role_judge", "intent_tuning"})


class PromptOptimizer:
    def __init__(self, data_dir: Any = None, enabled_provider: Any = None):
        self._path = Path(data_dir) / "prompt_optimizer.json" if data_dir else None
        self._enabled_provider = enabled_provider
        self._marks: dict[str, dict] = {}
        self._write_failures = 0
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8")) if self._path else {}
            self._marks = {k: v for k, v in raw.get("marks", {}).items()
                           if isinstance(k, str) and isinstance(v, dict)}
            self._marks = dict(list(self._marks.items())[-128:])
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def enabled(self) -> bool:
        try:
            return bool(self._enabled_provider()) if self._enabled_provider else True
        except Exception:
            return False

    @staticmethod
    def eligible(event: str, timeout_ms: Any = None) -> bool:
        return event in SHORT_EVENTS or (isinstance(timeout_ms, (int, float))
                                         and 0 < timeout_ms <= 10000)

    @staticmethod
    def _key(task: str, event: str, model: str) -> str:
        return json.dumps([task, event, model or "@task"], ensure_ascii=False)

    def prepare(self, prompt: Any, *, task: str, event: str, model: str = "",
                timeout_ms: Any = None) -> tuple[Any, bool]:
        if (not self.enabled() or not self.eligible(event, timeout_ms)
                or self._key(task, event, model) not in self._marks):
            return prompt, False
        if isinstance(prompt, str):
            return (prompt if DIRECT_OUTPUT_HINT in prompt else prompt + "\n" + DIRECT_OUTPUT_HINT), True
        if isinstance(prompt, (list, tuple)):
            messages = copy.deepcopy(list(prompt))
            # 独立文本消息，不改图片块，不修改调用方原始 prompt。
            if not any(isinstance(m, dict) and m.get("content") == DIRECT_OUTPUT_HINT for m in messages):
                messages.append({"role": "user", "content": DIRECT_OUTPUT_HINT})
            return messages, True
        return prompt, False

    def observe(self, result: Any, *, task: str, event: str, model: str = "",
                timeout_ms: Any = None, hinted: bool = False) -> None:
        if not self.enabled() or not self.eligible(event, timeout_ms) or not isinstance(result, dict):
            return
        has_reasoning = any(bool(result.get(k)) for k in ("reasoning", "reasoning_content"))
        body = result.get("response")
        if not has_reasoning and isinstance(body, str):
            try:
                structured = json.loads(body)
                has_reasoning = isinstance(structured, dict) and any(
                    bool(structured.get(k)) for k in ("reasoning", "reasoning_content"))
            except (ValueError, TypeError):
                pass
        if not has_reasoning:
            return
        actual_model = str(result.get("model") or result.get("model_name") or model or "")[:200]
        keys = [self._key(task, event, actual_model)]
        # SDK 只在响应后告知实际模型，调用前无法预解任务的内部路由。
        # 隐式路由约束该 task/event（包含宿主同任务内的兜底模型），显式模型独立。
        # 不强制 model 参数来冻结宿主路由，也不扩大到其他任务/事件。
        if not model:
            keys.append(self._key(task, event, ""))
        for key in keys:
            old = self._marks.get(key, {})
            self._marks[key] = {"model": actual_model, "observed": int(old.get("observed", 0)) + 1,
                                "ineffective": bool(hinted or old.get("ineffective"))}
        self._marks = dict(list(self._marks.items())[-128:])
        if self._path:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                temp = self._path.with_name(self._path.name + ".tmp")
                if self._path.is_symlink() or temp.is_symlink():
                    return
                temp.write_text(json.dumps({"version": 1, "marks": self._marks}, ensure_ascii=False), encoding="utf-8")
                os.replace(temp, self._path)
            except OSError:
                self._write_failures += 1

    def snapshot(self) -> dict:
        return {"enabled": self.enabled(), "marks": dict(self._marks), "write_failures": self._write_failures}
