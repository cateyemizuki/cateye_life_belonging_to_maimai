"""节假日服务（来源：虚拟恋人插件 holiday_service.py）。

获取指定日期的节假日/工作日信息，多级降级取数：

1. **整年放假安排表**（主源 timor.tech ``/api/holiday/year/{year}``）：
   一次请求拿到全年节假日 + 调休补班安排，含 ``holiday=false`` 的补班日；
   表落盘缓存（``<data_dir>/holiday_cache.json``，原子写），3 天 TTL，
   之后离线也能给出正确结果（重启后依然可用）。
2. **备源 jiejiariapi**（``/v1/holidays/{year}``）：字段为 ``isOffDay``。
3. **按天 API**（timor.tech ``/api/holiday/info/{date}``）：整年表拉不到时使用。
4. **本地星期几判断**：全部网络失败时的兜底（周一~周五为工作日）。

整年表里查不到的日期 = 没有特殊安排，按星期几判断（工作日 / 周末休息日）。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from core.storage import load_json, save_json_atomic

# 节假日 API 降级：周一~周五为工作日
HOLIDAY_FALLBACK_WEEKDAYS: set[int] = {0, 1, 2, 3, 4}


class HolidayService:
    """节假日信息服务（整年安排表 + 磁盘缓存 + 多级降级）。"""

    #: 整年安排表（主源，含调休补班）
    YEAR_API_TEMPLATE: str = "https://timor.tech/api/holiday/year/{year}"
    #: 整年安排表（备源）
    YEAR_API_FALLBACK_TEMPLATE: str = "https://api.jiejiariapi.com/v1/holidays/{year}"
    #: 按天查询（整年表不可用时的降级）
    INFO_API_TEMPLATE: str = "https://timor.tech/api/holiday/info/{date}"

    #: 整年表的磁盘缓存刷新间隔（秒）：3 天。安排表一年只发布几次，
    #: 3 天足够捕捉"新一年安排公布"这类变化，又不会频繁打网络。
    TABLE_TTL_SECONDS: float = 3 * 24 * 3600
    #: 内存缓存保留的日期条数
    MEMORY_CACHE_LIMIT: int = 7
    #: 网络请求超时（秒）
    TIMEOUT_SECONDS: float = 6.0
    #: 调休补班日的中文描述（备源拿不到"XX前/后补班"细名时使用）
    MAKEUP_FALLBACK_TEXT: str = "调休工作日（需要上班）"

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        """
        Args:
            data_dir: 插件数据目录；提供时把整年安排表落盘缓存，
                重启与离线场景仍可给出正确结果。
        """
        self._data_dir = Path(data_dir) if data_dir else None
        #: date -> 描述文本
        self._cache: Dict[str, str] = {}
        #: year -> {date: {"off": bool, "name": str, "kind": str}}
        self._tables: Dict[int, Dict[str, Dict[str, Any]]] = {}
        #: year -> 上次拉取时间（epoch 秒）
        self._table_fetched_at: Dict[int, float] = {}
        self._load_disk_cache()

    # ── 对外接口 ────────────────────────────────────────────────────────

    async def get_holiday_info(self, date: str) -> str:
        """获取指定日期的节假日/工作日信息（如 "工作日" / "国庆节假期"）。"""
        if date in self._cache:
            return self._cache[date]

        dt = self._parse_date(date)
        info: Optional[str] = None

        if dt is not None:
            table = await self._ensure_year_table(dt.year)
            if table is not None:
                info = self._describe(dt, table.get(date))

        if info is None:
            entry = await self._call_info_api(date)
            if entry is not None:
                dt_for_entry = dt if dt is not None else self._parse_date(date)
                info = self._describe(dt_for_entry, entry) if dt_for_entry else None

        if info is None:
            info = self._local_judge(date)

        self._cache[date] = info
        if len(self._cache) > self.MEMORY_CACHE_LIMIT:
            oldest = min(self._cache.keys())
            del self._cache[oldest]
        return info

    async def refresh(self, year: int) -> bool:
        """强制刷新某一年的安排表（忽略 TTL）。供排错/手动刷新使用。"""
        table = await self._fetch_year_table(year)
        if table is None:
            return False
        self._tables[year] = table
        self._table_fetched_at[year] = time.time()
        self._cache.clear()
        self._save_disk_cache()
        return True

    def table_size(self, year: int) -> int:
        """返回某年已缓存的特殊日期条数（0 表示没有该年数据）。"""
        return len(self._tables.get(year, {}))

    # ── 磁盘缓存（原子写）────────────────────────────────────────────────

    @property
    def _cache_file(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / "holiday_cache.json"

    def _load_disk_cache(self) -> None:
        """读取磁盘上的整年安排表缓存（失败静默忽略）。"""
        path = self._cache_file
        if not path:
            return
        payload = load_json(path, None)
        if not isinstance(payload, dict):
            return
        tables = payload.get("tables")
        if not isinstance(tables, dict):
            return
        for raw_year, entry in tables.items():
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            days = entry.get("days")
            if not isinstance(days, dict):
                continue
            self._tables[year] = {
                str(key): dict(value)
                for key, value in days.items()
                if isinstance(value, dict)
            }
            try:
                self._table_fetched_at[year] = float(entry.get("fetched_at") or 0.0)
            except (TypeError, ValueError):
                self._table_fetched_at[year] = 0.0

    def _save_disk_cache(self) -> None:
        """把整年安排表原子写入磁盘（失败静默忽略，绝不影响主流程）。"""
        path = self._cache_file
        if not path:
            return
        payload = {
            "version": 1,
            "tables": {
                str(year): {
                    "fetched_at": self._table_fetched_at.get(year, 0.0),
                    "days": days,
                }
                for year, days in self._tables.items()
            },
        }
        save_json_atomic(path, payload)

    # ── 整年表 ──────────────────────────────────────────────────────────

    async def _ensure_year_table(self, year: int) -> Optional[Dict[str, Dict[str, Any]]]:
        """取某一年的安排表；本地有新鲜缓存则直接用（拉取失败用过期旧表兜底）。"""
        cached = self._tables.get(year)
        fetched_at = self._table_fetched_at.get(year, 0.0)
        if cached is not None and (time.time() - fetched_at) < self.TABLE_TTL_SECONDS:
            return cached

        fresh = await self._fetch_year_table(year)
        if fresh is not None:
            self._tables[year] = fresh
            self._table_fetched_at[year] = time.time()
            self._save_disk_cache()
            return fresh

        # 拉取失败：过期的旧表也远好于"按星期几瞎猜"
        return cached

    async def _fetch_year_table(self, year: int) -> Optional[Dict[str, Dict[str, Any]]]:
        """联网拉取整年安排（主源失败自动换备源）。"""
        table = await self._fetch_year_table_timor(year)
        if table:
            return table
        return await self._fetch_year_table_jiejiariapi(year)

    async def _fetch_year_table_timor(self, year: int) -> Optional[Dict[str, Dict[str, Any]]]:
        """主源：timor.tech 整年安排表（含调休补班 ``holiday=false``）。"""
        data = await self._get_json(self.YEAR_API_TEMPLATE.format(year=year))
        if not isinstance(data, dict) or data.get("code") != 0:
            return None
        holidays = data.get("holiday")
        if not isinstance(holidays, dict) or not holidays:
            return None

        table: Dict[str, Dict[str, Any]] = {}
        for key, raw in holidays.items():
            if not isinstance(raw, dict):
                continue
            date = str(raw.get("date") or "").strip() or self._expand_key(str(key), year)
            if not date:
                continue
            name = str(raw.get("name") or "").strip()
            if raw.get("holiday") is True:
                table[date] = {"off": True, "name": name, "kind": "holiday"}
            else:
                table[date] = {"off": False, "name": name, "kind": "makeup"}
        return table or None

    async def _fetch_year_table_jiejiariapi(self, year: int) -> Optional[Dict[str, Dict[str, Any]]]:
        """备源：jiejiariapi 整年安排表（``isOffDay`` 表示是否放假）。

        备源把"小年"之类的非假日条目也一并列出，因此只把 ``isOffDay=False``
        **且落在周末**的条目录为调休（周末要上班 → 必然是调休），
        工作日的非假日条目直接忽略。
        """
        data = await self._get_json(self.YEAR_API_FALLBACK_TEMPLATE.format(year=year))
        if not isinstance(data, dict) or not data:
            return None

        table: Dict[str, Dict[str, Any]] = {}
        for key, raw in data.items():
            if not isinstance(raw, dict):
                continue
            date = str(raw.get("date") or key).strip()
            dt = self._parse_date(date)
            if dt is None:
                continue
            name = str(raw.get("name") or "").strip()
            is_off = raw.get("isOffDay")
            if is_off is True:
                table[date] = {"off": True, "name": name, "kind": "holiday"}
            elif is_off is False and dt.weekday() >= 5:
                # 周末却标注为上班 → 调休补班
                table[date] = {"off": False, "name": name, "kind": "makeup"}
        return table or None

    async def _call_info_api(self, date: str) -> Optional[Dict[str, Any]]:
        """按天查询（timor.tech）。

        正确读法：类型在 ``data["type"]["type"]``（0=工作日 1=周末 2=节日 3=调休）；
        ``data["holiday"]`` 里是细节（``holiday`` 布尔 + ``name``）。
        """
        data = await self._get_json(self.INFO_API_TEMPLATE.format(date=date))
        if not isinstance(data, dict) or data.get("code") != 0:
            return None

        holiday = data.get("holiday")
        if isinstance(holiday, dict):
            name = str(holiday.get("name") or "").strip()
            if holiday.get("holiday") is True:
                return {"off": True, "name": name, "kind": "holiday"}
            return {"off": False, "name": name, "kind": "makeup"}

        type_info = data.get("type")
        if not isinstance(type_info, dict):
            return None
        type_value = type_info.get("type")
        type_name = str(type_info.get("name") or "").strip()
        if type_value == 1:  # 周末
            return {"off": True, "name": "", "kind": "weekend"}
        if type_value == 2:  # 节日
            return {"off": True, "name": type_name, "kind": "holiday"}
        if type_value == 3:  # 调休（补班/放假）
            return {"off": False, "name": type_name, "kind": "makeup"}
        if type_value == 0:  # 工作日
            return {"off": False, "name": "", "kind": "workday"}
        return None

    async def _get_json(self, url: str) -> Optional[Any]:
        """带超时与异常兜底的 GET JSON。"""
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers={"User-Agent": "cateye-suite/0.1"})
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception:
            return None

    # ── 文本生成 ────────────────────────────────────────────────────────

    def _describe(self, dt: datetime, entry: Optional[Dict[str, Any]]) -> str:
        """把条目转成给 LLM 看的中文描述。"""
        if not entry:
            return self._local_judge(dt.strftime("%Y-%m-%d"))

        off = bool(entry.get("off"))
        name = str(entry.get("name") or "").strip()
        kind = str(entry.get("kind") or "")

        if off:
            if name:
                return f"{name}假期"
            return "休息日"

        if kind == "makeup":
            if "补班" in name or "调休" in name:
                return f"调休工作日（{name}）"
            if name:
                return f"调休工作日（{name}补班）"
            return self.MAKEUP_FALLBACK_TEXT
        if dt.weekday() >= 5:
            # 表里明确说了不放假，但又是周末 → 只能是调休
            return self.MAKEUP_FALLBACK_TEXT
        return "工作日"

    @staticmethod
    def _local_judge(date: str) -> str:
        """本地根据星期几判断工作日/休息日（最后的兜底）。"""
        dt = HolidayService._parse_date(date)
        if dt is None:
            return "工作日"
        if dt.weekday() in HOLIDAY_FALLBACK_WEEKDAYS:
            return "工作日"
        return "周末休息日"

    # ── 小工具 ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_date(date: str) -> Optional[datetime]:
        try:
            return datetime.strptime(str(date).strip(), "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _expand_key(key: str, year: int) -> str:
        """把 ``MM-DD`` 形式的键补成 ``YYYY-MM-DD``。"""
        normalized = key.strip()
        if not normalized:
            return ""
        if len(normalized) == 5 and normalized[2] == "-":
            return f"{year}-{normalized}"
        return normalized
