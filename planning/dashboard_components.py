"""Only declared read APIs are reachable from the sandboxed dashboard."""
from maibot_sdk import API
from .dashboard import build_life_dashboard


class LifeDashboardComponentsMixin:
    @API('life_dashboard', description='只读生活面板：状态历史、实际活动、睡眠与陪伴摘要', version='1')
    async def life_dashboard(self, hours: int = 24, day: str = "") -> dict:
        """``day``（可选，YYYY-MM-DD）：非空 → 结果附加 day 键（单日足迹/意图/睡眠/序列）；
        空串/非法 → 与既有 hours 路径逐字一致（非法值按空串处理并带 warning 字段）。"""
        return build_life_dashboard(self, hours=hours, day=day)
