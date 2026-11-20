"""Only declared APIs are reachable from the sandboxed dashboard."""
from maibot_sdk import API
from .dashboard import build_life_dashboard
from .dashboard_media import dashboard_thumbnail
from .dashboard_preferences import preference_editor_snapshot, save_preference_edit


class LifeDashboardComponentsMixin:
    @API('life_dashboard_interests', description='读取生活面板的可编辑视频兴趣', version='1', public=False)
    async def life_dashboard_interests(self) -> dict:
        return preference_editor_snapshot(self)

    @API('life_dashboard_save_interests', description='保存生活面板的视频兴趣', version='1', public=False)
    async def life_dashboard_save_interests(self, profile: str, revision: str, mode: str, entries_json: str = "[]") -> dict:
        return save_preference_edit(self, profile=profile, revision=revision, mode=mode, entries_json=entries_json)

    @API('life_dashboard_thumbnail', description='只读加载近期观看封面和动态配图', version='1')
    async def life_dashboard_thumbnail(self, image_id: str) -> dict:
        return {'src': await dashboard_thumbnail(image_id)}

    @API('life_dashboard', description='只读生活面板：状态历史、实际活动、睡眠与陪伴摘要', version='1')
    async def life_dashboard(self, hours: int = 24, usage_hours: int = 720,
                             usage_rows_limit: int = 0,
                             usage_rows_offset: int = 0,
                             usage_rows_level: str = "",
                             usage_rows_module: str = "",
                             day: str = "", video_recent_limit: int = 9,
                             post_recent_limit: int = 9) -> dict:
        """``day``（可选，YYYY-MM-DD）：非空 → 结果附加 day 键（单日足迹/意图/睡眠/序列）；
        空串/非法 → 与既有 hours 路径逐字一致（非法值按空串处理并带 warning 字段）。"""
        return build_life_dashboard(self, hours=hours, usage_hours=usage_hours,
                                    usage_rows_limit=usage_rows_limit,
                                    usage_rows_offset=usage_rows_offset,
                                    usage_rows_level=usage_rows_level,
                                    usage_rows_module=usage_rows_module, day=day,
                                    video_recent_limit=video_recent_limit,
                                    post_recent_limit=post_recent_limit)
