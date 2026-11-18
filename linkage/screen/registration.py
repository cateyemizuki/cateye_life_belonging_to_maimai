"""Trusted target lookup and the host's single-stream screen-tool registration refresh."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from core.targeting import private_qq_stream, target_qq_of


class TargetBindingCoordinator:
    """Own in-flight target revisions and one host registration refresh per binding."""

    def __init__(
        self,
        *,
        config_provider: Callable[[], Any],
        lookup_user: Callable[[str], Awaitable[Any]],
        private_streams: Callable[[], Awaitable[Any]],
        apply_target: Callable[[str, str], None],
        is_unloading: Callable[[], bool],
        plugin_id: str,
        reload_plugin: Callable[..., Awaitable[Any]] | None,
        logger: Any,
    ) -> None:
        self._config = config_provider
        self._lookup_user = lookup_user
        self._private_streams = private_streams
        self._apply_target = apply_target
        self._is_unloading = is_unloading
        self._plugin_id = plugin_id
        self._reload_plugin = reload_plugin
        self._logger = logger or logging.getLogger(__name__)
        self._revision = 0
        self._declared_stream: str | None = None
        self._bound_stream = ""
        self._binding_saved = False
        self._reload_requested_for: str | None = None
        self._reload_task: asyncio.Task | None = None

    def declare(self, stream_id: str) -> None:
        self._declared_stream = str(stream_id)

    def bound(self, stream_id: str, *, saved: bool) -> None:
        self._bound_stream = str(stream_id)
        self._binding_saved = saved

    async def resolve(self) -> str:
        target = target_qq_of(self._config())
        if not target:
            return ""
        try:
            result = await self._lookup_user(target)
            if isinstance(result, dict) and result.get("success") is not False:
                stream = private_qq_stream(result.get("stream") or result, target, queried_by_user=True)
                if stream:
                    return stream
        except Exception:
            pass
        try:
            streams = await self._private_streams()
            if isinstance(streams, dict):
                streams = streams.get("streams") or streams.get("data") or []
            for row in streams or []:
                stream = private_qq_stream(row, target)
                if stream:
                    return stream
        except Exception:
            pass
        return ""

    async def refresh(self, *, resolve: Callable[[], Awaitable[str]], clear_unresolved: bool = True) -> str:
        config = self._config()
        target = target_qq_of(config)
        self._revision += 1
        revision = self._revision
        stream = await resolve()
        if (
            revision != self._revision
            or config is not self._config()
            or target != target_qq_of(self._config())
            or self._is_unloading()
        ):
            return ""
        if stream or clear_unresolved:
            self._apply_target(target, stream)
        return stream

    def refresh_screen(self, *, enabled: bool) -> None:
        if self._is_unloading() or not self._binding_saved or self._declared_stream is None:
            return
        desired = self._bound_stream if enabled else ""
        if desired == self._declared_stream:
            return
        if self._reload_task is not None and not self._reload_task.done():
            return
        if self._reload_requested_for == desired:
            return
        if self._reload_plugin is None or not self._plugin_id:
            self._logger.warning("目标私聊范围变更，请在插件管理中重新加载以刷新屏幕工具")
            return
        self._reload_requested_for = desired
        self._reload_task = asyncio.create_task(self._refresh_declaration())

    async def _refresh_declaration(self) -> None:
        try:
            response = await self._reload_plugin(plugin_name=self._plugin_id)
            if response is False or (isinstance(response, dict) and response.get("success") is False):
                self._logger.warning("屏幕工具声明刷新未成功，可在插件管理中重新加载")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.warning("屏幕工具声明刷新失败，可在插件管理中重新加载：%s", exc)

    async def stop(self) -> None:
        self._revision += 1
        task = self._reload_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._reload_task = None
