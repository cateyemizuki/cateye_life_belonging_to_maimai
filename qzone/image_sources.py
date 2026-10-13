"""QQ 空间模块 · 配图收集层。

用途：按调用方给出的来源模式（宿主表情包库 / 本地图库 / 两者掷骰）为一条说说收集
配图，统一产出 ``list[bytes]``，交给发布层（``service.py`` 的 ``post_moment(images=...)``）
自行决定上传与拼参。本模块不持有 cookie、不认识 QQ 空间端点、不做任何网络请求。

收集层移植自 Maizone/MaiTrace（MIT）；失败一律静默降级，配图失败永不阻断发布。
收集层只产出 bytes，与发布层解耦。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: 单图上限（缓冲有界）：解码/读取结果超过此字节数一律丢弃。
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 图库扫描候选上限（内存有界）：蓄水池抽样只走过这么多候选项即停止。
LIBRARY_SCAN_LIMIT = 5000
#: 本地图库可识别的图片后缀（比对时统一小写）。
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

#: ``collect_images`` 接受的来源模式。
_VALID_MODES = ("emoji", "library", "random")
#: 每条说说配图数量的硬上限（与 Maizone ``max(1, min(4, n))`` 同口径）。
_MAX_PER_POST = 4


def peel_envelope(result: Any) -> Any:
    """剥宿主信封：``{"success": True, "data": {...}}`` → ``{...}``。

    仅当 ``result`` 是 dict、含 ``"data"`` 键，且（``success`` 为真 / ``status == "ok"`` /
    ``data`` 非 None）时下钻一层；其余形态原样返回，供调用方自行判断。
    """
    if isinstance(result, dict) and "data" in result:
        if result.get("success") or result.get("status") == "ok" or result.get("data") is not None:
            return result["data"]
    return result


def decode_base64_image(b64: Any) -> Optional[bytes]:
    """容错解码一张 base64 图片；空/非法/解码失败/超限 → None。

    会剥离 ``data:image/...;base64,`` 形式的 Data URL 前缀；``validate=False`` 容忍
    空白与杂散字符，但长度/填充非法仍会失败并降级为 None。
    """
    if not isinstance(b64, (str, bytes, bytearray)):
        return None
    try:
        if isinstance(b64, (bytes, bytearray)):
            text = bytes(b64).decode("ascii", "ignore")
        else:
            text = b64
        text = text.strip()
        if not text:
            return None
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1].strip()
        if not text:
            return None
        image = base64.b64decode(text, validate=False)
    except Exception as exc:  # binascii.Error / ValueError / UnicodeError ...
        logger.debug("表情包 base64 解码失败: %s", exc)
        return None
    if not image or len(image) > MAX_IMAGE_BYTES:
        return None
    return image


async def emoji_image(emoji_api: Any, description: str) -> Optional[bytes]:
    """经宿主表情包库取一张图（base64 → bytes）。任何失败返回 None，绝不抛出。

    ``emoji_api`` 为 None 或没有可调用的 ``get_by_description`` → None；
    调用异常 → None（记 debug，不 warning 洪泛）。返回值先剥信封再兼容两种形态：
    ``tuple`` 取 ``[0]``、``dict`` 取 ``"base64"``，其余形态按取不到处理。
    """
    getter = getattr(emoji_api, "get_by_description", None)
    if not callable(getter):
        return None
    try:
        result = await getter(description=description)
    except Exception as exc:
        logger.debug("ctx.emoji.get_by_description 异常: %s", exc)
        return None
    result = peel_envelope(result)
    if not result:
        return None
    if isinstance(result, tuple):
        b64 = result[0] if len(result) >= 1 else ""
    elif isinstance(result, dict):
        b64 = result.get("base64")
    else:
        return None
    if not b64:
        return None
    return decode_base64_image(b64)


def library_image(directory: Any) -> Optional[bytes]:
    """从本地图库目录随机取一张图。目录缺失/不可读/无图/超限 → None，绝不抛出。

    单趟 ``os.scandir`` 扫描，后缀（小写）在 ``IMAGE_SUFFIXES`` 内的文件为候选，
    候选数上限 ``LIBRARY_SCAN_LIMIT``；用蓄水池抽样（reservoir sampling，O(1) 内存）
    等概率选出 1 个，再读取其 bytes。
    """
    if not directory:
        return None
    try:
        root = Path(directory)
        if not root.is_dir():
            return None
    except Exception as exc:
        logger.debug("图库目录不可用 %r: %s", directory, exc)
        return None

    chosen: Optional[str] = None
    seen = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if seen >= LIBRARY_SCAN_LIMIT:
                    break
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if Path(entry.name).suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                seen += 1
                # 蓄水池抽样：第 n 个候选以 1/n 的概率替换当前选中项
                if random.randrange(seen) == 0:
                    chosen = entry.path
    except Exception as exc:
        logger.debug("图库扫描失败 %r: %s", directory, exc)
        return None

    if chosen is None:
        return None
    try:
        if os.path.getsize(chosen) > MAX_IMAGE_BYTES:
            logger.debug("图库图片超限，跳过: %s", chosen)
            return None
        data = Path(chosen).read_bytes()
    except Exception as exc:
        logger.debug("图库图片读取失败 %s: %s", chosen, exc)
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    return data


async def collect_images(
    *,
    mode: str,
    count: int,
    description: str,
    emoji_api: Any = None,
    library_dir: Any = None,
) -> List[bytes]:
    """收集本说说要配的图；恒返回 ``list[bytes]``（可能为空 = 本次发纯文本）。

    ``mode`` ∈ {"emoji", "library", "random"}，非法取空列表；``random`` 会以
    ``random.choice`` 在表情包与本地图库之间各半掷骰。``count`` 钳到 1~4。
    emoji 来源最多调用 ``count*2`` 次；library 来源最多尝试 ``count*2`` 次；
    两者均按 bytes 的 sha1 去重，命中 ``count`` 张即止。任何单次失败只跳过该张图。
    """
    images: List[bytes] = []
    try:
        normalized = str(mode or "").strip().lower()
        if normalized not in _VALID_MODES:
            logger.debug("配图来源模式非法，跳过配图: %r", mode)
            return []
        try:
            wanted = int(count)
        except (TypeError, ValueError):
            wanted = 1
        wanted = max(1, min(_MAX_PER_POST, wanted))

        source = normalized
        if source == "random":
            source = random.choice(("emoji", "library"))

        seen: set = set()
        for _ in range(wanted * 2):
            if len(images) >= wanted:
                break
            if source == "emoji":
                image = await emoji_image(emoji_api, description)
            else:
                image = library_image(library_dir)
            if not image:
                continue
            digest = hashlib.sha1(image).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            images.append(image)
    except Exception as exc:
        # 顶层兜底：配图失败永不阻断发布，返回已收集部分
        logger.warning("配图收集异常，降级为已收集的 %d 张: %s", len(images), exc)
    return images
