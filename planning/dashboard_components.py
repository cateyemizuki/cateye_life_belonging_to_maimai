"""Only declared read APIs are reachable from the sandboxed dashboard."""
from maibot_sdk import API
from .dashboard import build_life_dashboard


class LifeDashboardComponentsMixin:
    @API('life_dashboard', description='只读生活面板：状态历史、实际活动、睡眠与陪伴摘要', version='1')
    async def life_dashboard(self, hours: int = 24) -> dict:
        return build_life_dashboard(self, hours=hours)
