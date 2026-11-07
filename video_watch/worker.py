"""B 站观看流水线 worker（独立子进程入口，低优先级串行执行）。

用法：``python -B worker.py <spec.json> <result.json>``

流水线（2026-11-01 B 计划 §6 的诚实实现；B2 起含有界画面采样）：
1. 规范 BV（父侧已规范化，此处复核）；
2. 公开 view API 获取元信息（标题/UP主/时长/简介）——失效/需登录/受限按
   真实错误码归类，不编造；
3. 时长上限校验：超出预算的视频明确拒绝，不宣称能完整观看；
4. 公开字幕（CC）文本抓取（可选，拿不到就如实标注缺失）；
5. 画面采样（仅 spec.visual 请求时尝试，任一步不可得都如实降级不阻塞）：
   playurl 低清直链 → 限额下载 → ffmpeg 等间隔抽帧（自开头、帧数有界）。
   宿主侧 ``llm.generate`` 消息列表支持 image base64 片段（B0 取证：
   src/services/llm_service.py 解析链），vlm 摘要在父侧完成——本子进程
   只产出帧，不触碰 SDK/凭据；无 ffmpeg / 下载超限 / 受限视频如实标注。
6. 音频 = not_attempted：无音轨转写通道，不编造对白/音乐。

进度协议：progress.json（stage/detail）；结果协议：result.json（status
completed/failed + 结构化结果）。所有网络请求有超时；worker 主体仅标准库，
拼图路径经 video_watch.sampling 依赖 Pillow（_manifest.json 已声明；缺席时
画面采样如实降级）；ffmpeg 为可选外部依赖，缺席只降级画面采样。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any
from pathlib import Path
from urllib import parse as _urlparse
from urllib import request as _urlrequest

_UA = "bili-watch/0.1 (personal companion plugin)"
_HTTP_TIMEOUT = 10.0
_SUBTITLE_TEXT_LIMIT = 20000

#: 画面采样能力标记（记录实际采样口径，旧记忆不会冒充重新观看）
VISUAL_CAPABILITY = "frames-v2-page"
#: 单帧 base64 上限（字节）：超出的帧丢弃计数，不无界膨胀 result/回执
_FRAME_BYTES_LIMIT = 96 * 1024
#: 抽帧画面宽度（等比缩放；有界采样不是逐帧观看）
_FRAME_WIDTH = 480

#: PR-A1 空间高清截图：单帧字节上限（原生分辨率高质量 JPEG；超出如实弃用）
_QZONE_FRAME_BYTES_LIMIT = 8 * 1024 * 1024
#: PR-A1 黑屏/纯色判定：灰度缩略图像素标准差低于该值视为无内容帧
_QZONE_BLANK_STDDEV = 2.0

#: view API 错误码 → 面向结果契约的失败原因（如实归类，不虚构）
_VIEW_API_ERRORS = {
    -400: "请求错误",
    -403: "权限不足（可能需要登录/会员/地区权限）",
    -404: "视频不存在或已删除",
    62002: "稿件不可见（UP主设置为不可见）",
    62004: "稿件审核中",
}


def _http_get_json(url: str) -> dict:
    request = _urlrequest.Request(url, headers={"User-Agent": _UA})
    with _urlrequest.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _write_progress(job_dir: Path, stage: str, detail: str = "") -> None:
    try:
        (job_dir / "progress.json").write_text(
            json.dumps({"stage": stage, "detail": detail, "updated_at": time.time()},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _fail(result_path: Path, task_id: str, error_class: str, error: str) -> None:
    result_path.write_text(json.dumps({
        "task_id": task_id, "status": "failed", "error_class": error_class,
        "error": error, "finished_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")


def _selected_page(data: dict) -> dict:
    """Bind duration and CID to the same page, never the collection duration."""
    pages = data.get("pages") or []
    if pages:
        if not isinstance(pages, list) or any(not isinstance(p, dict) for p in pages):
            raise LookupError("视频分P信息异常，无法确定观看范围")
        cid = str(data.get("cid") or "")
        index, page = next(((i, p) for i, p in enumerate(pages)
                            if cid and str(p.get("cid") or "") == cid), (0, pages[0]))
    else:
        index, page = 0, data
    try:
        duration = int(page.get("duration") or 0)
        number = int(page.get("page") or index + 1)
    except (TypeError, ValueError):
        raise LookupError("所选分P时长未知，拒绝用合集总时长推断")
    selected_cid = str(page.get("cid") or "")
    if duration <= 0 or not selected_cid or number <= 0:
        raise LookupError("所选分P时长或CID未知，无法确定观看范围")
    return {"cid": selected_cid, "duration_seconds": duration,
            "page_number": number, "page_count": len(pages) if pages else 1,
            "page_title": str(page.get("part") or page.get("title") or "")[:300]}


def _fetch_metadata(bvid: str) -> dict:
    payload = _http_get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    code = payload.get("code")
    if code != 0:
        reason = _VIEW_API_ERRORS.get(code, f"接口返回 code={code}")
        raise LookupError(f"视频不可观看：{reason}")
    data = payload.get("data") or {}
    return {
        "bvid": str(data.get("bvid") or bvid),
        "aid": str(data.get("aid") or ""),
        **_selected_page(data),
        "title": str(data.get("title") or ""),
        "uploader": str((data.get("owner") or {}).get("name") or ""),
        "desc": str(data.get("desc") or ""),
        # 公开封面图 URL（卡片分享 picUrl 用；非播放鉴权直链，不进文字缓存）
        "pic": str(data.get("pic") or ""),
        "url": f"https://www.bilibili.com/video/{data.get('bvid') or bvid}",
        "published_at_ts": int(data.get("pubdate") or 0),
    }


def _fetch_public_subtitle(aid: str, cid: str) -> dict:
    """公开 CC 字幕文本（无登录凭据；拿不到如实返回缺失）。"""
    if not aid or not cid:
        return {"status": "missing", "reason": "缺少 aid/cid，未尝试字幕获取"}
    try:
        player = _http_get_json(f"https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}")
    except Exception as exc:
        return {"status": "missing", "reason": f"字幕接口不可达：{exc}"}
    if player.get("code") != 0:
        return {"status": "missing", "reason": f"字幕接口返回 code={player.get('code')}"}
    subtitles = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not subtitles:
        return {"status": "missing", "reason": "该视频没有公开 CC 字幕"}
    sub_url = str((subtitles[0] or {}).get("subtitle_url") or "")
    if not sub_url:
        return {"status": "missing", "reason": "字幕条目无下载地址"}
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    try:
        body = _http_get_json(sub_url)
    except Exception as exc:
        return {"status": "missing", "reason": f"字幕下载失败：{exc}"}
    lines = [str(item.get("content") or "") for item in (body.get("body") or [])]
    text = " ".join(line for line in lines if line)[:_SUBTITLE_TEXT_LIMIT]
    if not text:
        return {"status": "missing", "reason": "字幕内容为空"}
    return {"status": "available", "chars": len(text), "text": text,
            "lang": str((subtitles[0] or {}).get("lan") or "")}


def _fetch_playurl(bvid: str, cid: Any) -> str:
    """低清直链（playurl 公开接口，platform=html5 + qn=16 不要求登录）。

    Returns:
        视频直链 URL。
    Raises:
        LookupError: 接口拒绝（受限/需要登录/会员等，带真实原因）。
    """
    url = ("https://api.bilibili.com/x/player/playurl?bvid="
           + _urlparse.quote(bvid) + "&cid=" + _urlparse.quote(str(cid or ""))
           + "&qn=16&platform=html5&type=mp4")
    payload = _http_get_json(url)
    code = payload.get("code")
    if code != 0:
        raise LookupError(_VIEW_API_ERRORS.get(code, f"playurl code={code}"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    durl = data.get("durl") if isinstance(data.get("durl"), list) else []
    if len(durl) > 1:
        raise LookupError("媒体包含多个分段，当前采样器不拼接分段；画面采样未执行")
    first = durl[0] if durl and isinstance(durl[0], dict) else {}
    direct = str(first.get("url") or "").strip()
    if not direct:
        raise LookupError("playurl 未返回可用直链（可能为 DASH/需要更高权限的格式）")
    return direct


def _fetch_playurl_hq(bvid: str, cid: Any) -> tuple:
    """公开高清直链（playurl qn=64 + platform=html5，**不携带任何登录态**）。

    已实测（2026-11-07，BV1bexB69Es7/cid43189536170，未登录 html5）：qn=64 →
    quality=64（720P）；qn=32 反而回落 16（360P），qn=80 封顶 64——故直接请求 64，
    不试 32。逐视频可用性不同，调用方必须实测帧尺寸兜底，不信请求参数。

    Returns:
        (direct_url, reported_qn)：直链与接口自报画质（仅诊断用，非尺寸依据）。
    Raises:
        LookupError: 接口拒绝/需登录/多分段等，带真实原因（不绕权限）。
    """
    url = ("https://api.bilibili.com/x/player/playurl?bvid="
           + _urlparse.quote(bvid) + "&cid=" + _urlparse.quote(str(cid or ""))
           + "&qn=64&platform=html5&type=mp4")
    payload = _http_get_json(url)
    code = payload.get("code")
    if code != 0:
        raise LookupError("hd_unavailable: " + _VIEW_API_ERRORS.get(code, f"playurl code={code}"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    durl = data.get("durl") if isinstance(data.get("durl"), list) else []
    if len(durl) > 1:
        raise LookupError("hd_unavailable: 媒体包含多个分段，不拼接分段提取截图")
    first = durl[0] if durl and isinstance(durl[0], dict) else {}
    direct = str(first.get("url") or "").strip()
    if not direct:
        raise LookupError("hd_unavailable: playurl 未返回可用直链（可能为 DASH/需要更高权限的格式）")
    try:
        reported_qn = int(data.get("quality") or 0)
    except (TypeError, ValueError):
        reported_qn = 0
    return direct, reported_qn


def _jpeg_dimensions(data: bytes) -> tuple:
    """读 JPEG SOF 段取真实像素尺寸（不依赖解码器/请求参数）；读不出返回 None。"""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                   0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker == 0xDA or marker == 0xD9:  # 扫描开始/结束仍未见 SOF
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # 无长度段
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            return None
        if marker in sof_markers:
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return (width, height) if width > 0 and height > 0 else None
        index += 2 + length
    return None


def _frame_blank_reason(data: bytes) -> str:
    """确定性黑屏/纯色检查（灰度缩略图像素标准差）；返回空串=通过。

    Pillow 缺席或解码失败时如实返回不可用原因（调用方弃帧，不冒充已检查）。
    """
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return "质量检查不可用（缺 Pillow）"
    try:
        with Image.open(io.BytesIO(data)) as image:
            gray = image.convert("L")
            gray.thumbnail((64, 64))
            stddev = ImageStat.Stat(gray).stddev[0]
    except Exception:
        return "帧解码失败"
    return "" if stddev >= _QZONE_BLANK_STDDEV else "黑屏/纯色帧"


def _pick_asset_times(ok_times: list, max_assets: int) -> list:
    """从实际取得帧的时间点里选分散点位：中/末/首优先，不默认第一帧。"""
    times = sorted({float(t) for t in ok_times
                    if isinstance(t, (int, float)) and t >= 0})  # NaN>=0 为 False，自然排除
    if not times:
        return []
    limit = max(1, int(max_assets))
    if limit == 1 or len(times) == 1:
        return [times[len(times) // 2]]
    picks: list = []
    for index in (len(times) // 2, len(times) - 1, 0):
        if times[index] not in picks:
            picks.append(times[index])
    rest = [t for t in times if t not in picks]
    while rest and len(picks) < limit:
        picks.append(rest.pop(len(rest) // 2))
    return sorted(picks[:limit])


def _extract_qzone_assets(spec: dict, video: dict, job_dir: Path,
                          deadline_epoch: float, ok_times: list) -> dict:
    """PR-A1：高清非拼图截图资产提取（仅 spec 带 qzone_assets 时由调用方触发）。

    原生分辨率单帧（无 scale 滤镜）+ 实测尺寸兜底；低清/黑屏/重复如实弃用；
    写入父侧传入的受控绝对目录，产出 reserved manifest（不含播放直链）。
    任何失败都返回 {"assets": [], "diag": 原因}，绝不抛出拖挂 VLM 主结果。
    """
    cfg = spec.get("qzone_assets") if isinstance(spec.get("qzone_assets"), dict) else {}
    notes: list = []
    measured: list = []
    reported_qn: Any = None
    tmp = job_dir / "hq_asset_tmp.jpg"
    try:
        try:
            min_width = max(320, min(3840, int(cfg.get("min_width") or 1280)))
            max_assets = max(1, min(4, int(cfg.get("max_assets") or 2)))
            ttl_hours = max(1, min(168, int(cfg.get("ttl_hours") or 24)))
        except (TypeError, ValueError):
            raise LookupError("hd_unavailable: 截图参数非法")
        root = Path(str(cfg.get("asset_dir") or ""))
        if not str(cfg.get("asset_dir") or "") or not root.is_absolute() or root.is_symlink():
            raise LookupError("hd_unavailable: 资产目录不可用（需绝对路径且非 symlink）")
        root = root.resolve()
        decoder = _decoder_executable(job_dir)
        if not decoder:
            raise LookupError("hd_unavailable: 本机无 ffmpeg，未提取空间截图")
        times = _pick_asset_times(ok_times, max_assets)
        if not times:
            raise LookupError("hd_unavailable: 无可用采样时间点")
        direct, reported_qn = _fetch_playurl_hq(str(video.get("bvid") or ""), video.get("cid"))
        parsed = _urlparse.urlsplit(direct)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LookupError("hd_unavailable: 媒体地址协议不可用")
        root.mkdir(parents=True, exist_ok=True)
        assets: list = []
        seen_hashes: set = set()
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                 if sys.platform == "win32" else 0)
        for at in times:
            remaining = deadline_epoch - time.time()
            if remaining <= 0:
                notes.append("到达任务总截止，停止提取")
                break
            tmp.unlink(missing_ok=True)
            # 原生分辨率单帧：无 -vf/scale（红线：禁止低清放大冒充高清）；
            # 直链只进 ffmpeg 命令行，不落 result/manifest。
            command = [decoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-threads", "1", "-filter_threads", "1",
                       "-rw_timeout", "15000000", "-user_agent", _UA,
                       "-headers", "Referer: https://www.bilibili.com/\r\n",
                       "-protocol_whitelist", "http,https,tcp,tls,crypto",
                       "-ss", str(at), "-i", direct, "-an", "-sn",
                       "-frames:v", "1", "-q:v", "2", str(tmp)]
            try:
                process = subprocess.run(command, capture_output=True,
                                         timeout=min(30.0, remaining), creationflags=flags)
            except subprocess.TimeoutExpired:
                notes.append(f"t={at}s 抽帧超时")
                continue
            if process.returncode != 0 or not tmp.exists():
                notes.append(f"t={at}s 抽帧失败")
                continue
            data = tmp.read_bytes()
            tmp.unlink(missing_ok=True)
            if not data or len(data) > _QZONE_FRAME_BYTES_LIMIT:
                notes.append(f"t={at}s 帧字节超出上限")
                continue
            size = _jpeg_dimensions(data)
            if size is None:
                notes.append(f"t={at}s 无法读出 JPEG 尺寸")
                continue
            width, height = size
            measured.append(f"{width}x{height}@{at}s")
            if width < min_width:
                notes.append(f"t={at}s 实测宽 {width}<{min_width}，低清弃用")
                continue
            blank = _frame_blank_reason(data)
            if blank:
                notes.append(f"t={at}s {blank}")
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                notes.append(f"t={at}s 重复帧（hash 相同）")
                continue
            asset_id = "vframe_" + uuid.uuid4().hex
            final = root / (asset_id + ".jpg")
            if final.is_symlink() or final.resolve().parent != root:
                raise LookupError("hd_unavailable: 资产路径越出受控目录，拒绝写入")
            with open(final, "xb") as handle:
                handle.write(data)
            seen_hashes.add(digest)
            now = time.time()
            assets.append({"asset_id": asset_id, "bvid": str(video.get("bvid") or ""),
                           "cid": str(video.get("cid") or ""),
                           "task_id": str(spec.get("task_id") or ""),
                           "timestamp_seconds": float(at), "width": width, "height": height,
                           "mime": "image/jpeg", "sha256": digest, "bytes": len(data),
                           "status": "reserved", "created_at": now,
                           "expires_at": now + ttl_hours * 3600})
            if len(assets) >= max_assets:
                break
        if not assets:
            detail = "；".join(notes) or "未取得合格帧"
            raise LookupError(f"hd_unavailable: reported_qn={reported_qn}，"
                              f"实测 {'/'.join(measured) or '无'}；{detail}")
        return {"assets": assets, "diag": "；".join(notes)[:350]}
    except LookupError as exc:
        return {"assets": [], "diag": str(exc)[:350]}
    except Exception as exc:  # 提取失败不拖挂 VLM 主结果，如实记录类型
        return {"assets": [], "diag": f"hd_unavailable: 提取异常（{type(exc).__name__}）"[:350]}
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _download_bounded(url: str, target: Path, max_bytes: int,
                      deadline_epoch: float) -> int:
    """有界下载：超预算（字节/截止时刻）即停止并抛出，不无界落盘。"""
    request = _urlrequest.Request(url, headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
    downloaded = 0
    with _urlrequest.urlopen(request, timeout=_HTTP_TIMEOUT) as response, \
            open(target, "wb") as handle:
        while True:
            if time.time() >= deadline_epoch:
                raise LookupError("媒体下载超过任务总时限，采样终止（如实标注）")
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise LookupError(f"媒体下载超出预算 {max_bytes} 字节，采样终止（如实标注）")
            handle.write(chunk)
    if downloaded <= 0:
        raise LookupError("媒体下载为空")
    return downloaded


def _decoder_executable(job_dir: Path) -> str:
    """Use PATH or this plugin's provisioned decoder; never change the host PATH."""
    configured = shutil.which("ffmpeg")
    if configured:
        return configured
    local = job_dir.parent.parent / "decoder" / "ffmpeg.exe"
    return str(local) if local.is_file() else ""


def _extract_frames(media_path: Path, job_dir: Path, duration: int, max_frames: int,
                    deadline_epoch: float) -> tuple:
    """Seek to explicit target times; bound every decoder call by the remaining deadline."""
    decoder = _decoder_executable(job_dir)
    if not decoder:
        raise LookupError("本机无 ffmpeg，画面采样未执行（如实标注）")
    interval = max(1, int(duration) // (int(max_frames) + 1))
    frames_dir = job_dir / "frames"
    if frames_dir.is_symlink() or frames_dir.resolve().parent != job_dir.resolve():
        raise LookupError("抽帧目录越出本任务，拒绝处理")
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames, sampled_seconds = [], []
    for index in range(int(max_frames)):
        remaining = deadline_epoch - time.time()
        if remaining <= 0:
            raise LookupError("ffmpeg 抽帧超过任务总时限")
        at = min(max(0.0, float(duration) - 0.05), (index + 1) * interval)
        path = frames_dir / f"frame_{index:03d}.jpg"
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                 if sys.platform == "win32" else 0)
        try:
            proc = subprocess.run(
                [decoder, "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
                 "-ss", str(at), "-i", str(media_path), "-an", "-sn",
                 "-vf", f"scale={_FRAME_WIDTH}:-2", "-frames:v", "1", "-q:v", "7", str(path)],
                capture_output=True, timeout=min(30.0, remaining), creationflags=flags)
        except subprocess.TimeoutExpired:
            raise LookupError("ffmpeg 抽帧超过任务总时限")
        if proc.returncode != 0:
            raise LookupError(f"ffmpeg 抽帧失败（exit={proc.returncode}）")
        data = path.read_bytes() if path.exists() else b""
        path.unlink(missing_ok=True)
        if not data or len(data) > _FRAME_BYTES_LIMIT:
            continue
        sampled_seconds.append(at)
        frames.append({"t": at, "image_format": "jpeg",
                       "image_base64": base64.b64encode(data).decode("ascii")})
    if not frames:
        raise LookupError("ffmpeg 未产出有效帧（媒体可能损坏或编码不受支持）")
    return frames, sampled_seconds, interval


def _sample_visual_legacy(spec: dict, video: dict, job_dir: Path,
                   deadline_epoch: float) -> dict:
    """有界画面采样入口：任一步不可得都如实降级，不影响文本来源结果。

    Returns:
        dict：completed（含 frames/capability）或 failed/not_attempted（含 reason）。
    """
    cfg = spec.get("visual") if isinstance(spec.get("visual"), dict) else None
    if not cfg:
        return {"status": "not_attempted",
                "reason": "本任务按纯文本来源流水线执行（未请求画面采样）"}
    max_frames = max(1, min(12, int(cfg.get("max_frames") or 6)))
    max_bytes = max(4, min(128, int(cfg.get("max_download_mb") or 32))) * 1024 * 1024
    if not _decoder_executable(job_dir):
        return {"status": "not_attempted",
                "reason": "本机无 ffmpeg，画面采样未执行（如实标注）",
                "capability": VISUAL_CAPABILITY}
    try:
        _write_progress(job_dir, "visual_media", str(video.get("bvid") or ""))
        direct = _fetch_playurl(str(video.get("bvid") or ""), video.get("cid"))
        media_path = job_dir / "media.mp4"
        try:
            _download_bounded(direct, media_path, max_bytes, deadline_epoch)
            _write_progress(job_dir, "visual_frames", str(video.get("bvid") or ""))
            frames, sampled_seconds, interval = _extract_frames(
                media_path, job_dir, int(video.get("duration_seconds") or 0),
                max_frames, deadline_epoch)
        finally:
            try:
                media_path.unlink(missing_ok=True)
            except Exception:
                pass
    except LookupError as exc:
        return {"status": "failed", "reason": str(exc), "capability": VISUAL_CAPABILITY}
    except Exception as exc:  # 网络等未预期异常：如实降级，不阻塞文本来源结果
        return {"status": "failed", "reason": f"画面采样异常：{exc}",
                "capability": VISUAL_CAPABILITY}
    return {"status": "completed", "capability": VISUAL_CAPABILITY,
            "frames": frames, "sampled_seconds": sampled_seconds,
            "frame_interval_seconds": interval,
            "note": f"按目标时间点间隔 {interval}s 定点采样 {len(frames)} 帧"}


def _sample_visual(spec: dict, video: dict, job_dir: Path, deadline_epoch: float) -> dict:
    cfg = spec.get("visual") or {}
    if "max_montages" not in cfg:
        return _sample_visual_legacy(spec, video, job_dir, deadline_epoch)
    from video_watch.sampling import sample_plan, pack_montages, capability
    cap = capability(cfg)
    decoder = _decoder_executable(job_dir)
    if not decoder:
        return {"status": "not_attempted", "reason": "本机无解码器", "capability": cap}
    source_mode = str(cfg.get("source_mode", "stream"))
    sampling_mode = str(cfg.get("sampling_mode", "seek"))
    if source_mode not in ("stream", "download") or sampling_mode not in ("seek", "paced"):
        return {"status": "failed", "reason": "未知媒体读取/采样模式", "capability": cap}
    try:
        plan = sample_plan(float(video.get("duration_seconds") or 0),
                           int(cfg.get("interval_seconds") or 25),
                           min(32, max(1, int(cfg.get("max_montages") or 16))))
    except ValueError as exc:
        return {"status": "failed", "reason": str(exc), "capability": cap, "error_class": "montage_limit"}
    folder = job_dir / "frames"
    if folder.is_symlink() or folder.resolve().parent != job_dir.resolve():
        return {"status": "failed", "reason": "抽帧目录不安全", "capability": cap}
    folder.mkdir(parents=True, exist_ok=True)
    media = job_dir / "media.mp4"
    paths = []
    started = time.monotonic()
    try:
        _write_progress(job_dir, "visual_media", f"{source_mode}: {plan['frame_count']} frames / {plan['montage_count']} images")
        direct = _fetch_playurl(str(video.get("bvid") or ""), video.get("cid"))
        parsed = _urlparse.urlsplit(direct)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LookupError("媒体地址协议不可用")
        source = direct
        if source_mode == "download":
            _download_bounded(direct, media, max(4, min(1024, int(cfg.get("max_download_mb") or 256))) * 1024 * 1024, deadline_epoch)
            source = str(media)
        # 采样韧性（2026-11-04 视频链路补充修复1）：单点超时/读取失败先重试一次，仍失败
        # 则跳过该点继续其余采样点——部分拼图优于全无；仅当全部采样点都失败才降级
        # text_sources。到达任务总截止即停止采样并用已取得的帧出图（不突破时限/下载
        # 限额，也不自动改成全片下载）。
        ok_paths, ok_times = [], []
        for index, at in enumerate(plan["timestamps"]):
            if sampling_mode == "paced":
                wait = started + at - time.monotonic()
                if wait > 0:
                    if time.time() + wait >= deadline_epoch:break
                    time.sleep(wait)
            remaining = deadline_epoch - time.time()
            if remaining <= 0:break
            path = folder / f"sample_{index:04d}.jpg";paths.append(path)
            command = [decoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-threads", "1", "-filter_threads", "1"]
            if source_mode == "stream":
                command += ["-rw_timeout", "15000000", "-user_agent", _UA,
                            "-headers", "Referer: https://www.bilibili.com/\r\n",
                            "-protocol_whitelist", "http,https,tcp,tls,crypto"]
            command += ["-ss", str(at), "-i", source, "-an", "-sn", "-vf", "scale=480:-2",
                        "-frames:v", "1", "-threads", "1", "-q:v", "7", str(path)]
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 0)
            got = False
            for _attempt in range(2):  # 首次 + 至多一次重试
                remaining = deadline_epoch - time.time()
                if remaining <= 0:break
                try:process = subprocess.run(command, capture_output=True, timeout=min(30, remaining), creationflags=flags)
                except subprocess.TimeoutExpired:continue
                if process.returncode or not path.exists() or not 0 < path.stat().st_size <= _FRAME_BYTES_LIMIT:
                    continue
                got = True;break
            if got:
                ok_paths.append(path);ok_times.append(at)
                _write_progress(job_dir, "visual_frames", f"{index+1}/{plan['frame_count']}")
        if not ok_paths:
            raise LookupError(f"全部{plan['frame_count']}个采样点均未取得有效帧；不自动改成全片下载")
        frames = pack_montages(ok_paths, ok_times)
        partial = ("" if len(ok_paths) == plan["frame_count"] else
                   f"（计划{plan['frame_count']}个采样点，实际取得{len(ok_paths)}帧）")
        completed = {"status": "completed", "capability": cap, "frames": frames,
                     "sampled_seconds": ok_times, "frame_count": len(ok_paths),
                     "montage_count": len(frames), "source_mode": source_mode, "sampling_mode": sampling_mode,
                     "frame_interval_seconds": int(cfg.get("interval_seconds") or 25),
                     "note": f"实际采样{len(ok_paths)}帧，合为{len(frames)}张4×4拼图{partial}"}
        # PR-A1：VLM 帧已取得、finally 清理之前提取空间高清截图（仅 spec 带配置时；
        # shadow/off 父侧不装配 qzone_assets → 此分支零开销）。失败隔离，不拖挂主结果。
        if isinstance(spec.get("qzone_assets"), dict) and spec["qzone_assets"]:
            try:
                qzone = _extract_qzone_assets(spec, video, job_dir, deadline_epoch, ok_times)
            except Exception as exc:
                qzone = {"assets": [],
                         "diag": f"空间截图提取异常（{type(exc).__name__}），不影响画面理解"[:350]}
            completed["qzone_assets"] = qzone.get("assets") or []
            if qzone.get("diag"):
                completed["qzone_assets_diag"] = str(qzone["diag"])[:350]
        return completed
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (LookupError, ValueError)) else f"采样失败（{type(exc).__name__}），未发送原始媒体地址"
        return {"status": "failed", "reason": reason[:350], "capability": cap,
                "source_mode": source_mode, "frame_count_read": sum(p.exists() for p in paths),
                "expected_montages": plan["montage_count"]}
    finally:
        for path in paths:path.unlink(missing_ok=True)
        media.unlink(missing_ok=True)


def run(spec: dict, result_path: Path) -> None:
    task_id = str(spec.get("task_id") or "")
    job_dir = result_path.parent
    bvid = str(spec.get("bvid") or "")
    result_path.parent.mkdir(parents=True, exist_ok=True)

    _write_progress(job_dir, "resolve", bvid)
    if not bvid or not (len(bvid) == 12 and bvid.startswith("BV")):
        _fail(result_path, task_id, "invalid_input", f"worker 收到非法 BV 标识: {bvid!r}")
        return

    _write_progress(job_dir, "metadata", bvid)
    try:
        video = _fetch_metadata(bvid)
    except LookupError as exc:
        _fail(result_path, task_id, "unsupported_video", str(exc))
        return
    except Exception as exc:
        _fail(result_path, task_id, "network", f"元信息获取失败：{exc}")
        return

    max_seconds = int(spec.get("max_video_duration_seconds", 1800))
    duration = int(video.get("duration_seconds") or 0)
    if duration <= 0:
        _fail(result_path, task_id, "unsupported_video", "视频时长未知，无法校准观看预算")
        return
    if max_seconds > 0 and duration > max_seconds:
        _fail(result_path, task_id, "unsupported_video",
              f"视频时长 {duration}s 超出本任务预算上限 {max_seconds}s；按计划不宣称能完整观看，已拒绝")
        return

    _write_progress(job_dir, "subtitles", bvid)
    subtitle = _fetch_public_subtitle(video.get("aid", ""), video.get("cid", ""))

    _write_progress(job_dir, "summarize", bvid)
    observations: list = [f"标题：{video['title']}", f"UP主：{video['uploader']}",
                          f"所选分P时长：{duration}s"]
    if video["page_count"] > 1:
        observations.append(f"仅处理第{video['page_number']}/{video['page_count']}P："
                            f"{video['page_title']}；未观看其他分P")
    if video.get("desc"):
        observations.append("简介：" + video["desc"][:400])
    if subtitle.get("status") == "available":
        observations.append(f"公开字幕（{subtitle.get('lang') or '未知语言'}）已读取 {subtitle['chars']} 字")

    # B2 有界画面采样：任一步不可得都如实降级，不阻塞文本来源结果
    visual = _sample_visual(spec, video, job_dir, float(spec.get("deadline_epoch") or 0))
    # PR-A1：manifest/诊断从 visual 提到 result 顶层（元数据，不含二进制与直链），
    # 父侧 manager._finish 的 visual.pop("frames") 边界不受影响。
    qzone_assets = visual.pop("qzone_assets", None)
    qzone_assets_diag = visual.pop("qzone_assets_diag", None)
    visual_completed = visual.get("status") == "completed"
    if visual_completed:
        observations.append(f"画面采样：{len(visual.get('frames') or [])} 帧"
                            f"（{visual.get('note') or ''}）")
    # 采样失败/未尝试只留在内部诊断（visual.status/reason），不写进 observations，
    # 避免“画面采样失败”类声明经观后感/描述提示词外泄（2026-11-04 措辞修复）。

    source_detail = ("实际读取标题/简介与公开CC字幕文本" if subtitle.get("status") == "available"
                     else "实际读取标题/简介；公开CC字幕未取得")
    result = {
        "task_id": task_id,
        "status": "completed",
        "video": {k: video[k] for k in ("bvid", "aid", "cid", "title", "uploader",
                                        "duration_seconds", "url", "page_number",
                                        "page_count", "page_title", "desc")},
        "watched_range": {
            "mode": "sampled_frames" if visual_completed else "text_sources",
            "detail": (source_detail + "，并做等间隔画面采样"
                       "（非逐帧观看）" if visual_completed
                       else source_detail + "；未逐帧观看画面"),
        },
        "sources": {"metadata": "bilibili 公开 view API", "subtitle": subtitle.get("status")},
        "visual": visual,
        "audio": {"status": "not_attempted", "reason": "无音轨转写通道，不编造对白/音乐"},
        "subtitle_text": subtitle.get("text", "") if subtitle.get("status") == "available" else "",
        "observations": observations,
        "impression": None,
        "error_class": None,
        "error": None,
        "started_at": float(spec.get("started_at") or 0),
        "finished_at": time.time(),
        "cached": False,
    }
    if qzone_assets is not None:
        result["qzone_assets"] = qzone_assets
    if qzone_assets_diag:
        result["qzone_assets_diag"] = str(qzone_assets_diag)[:350]
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    _write_progress(job_dir, "done", "completed")


def main(argv: list) -> None:
    if len(argv) != 3:
        print("usage: worker.py <spec.json> <result.json>")
        raise SystemExit(2)
    # 允许以脚本方式直接运行：把包父目录加入 sys.path 以便复用 bv 规范化
    package_parent = Path(__file__).resolve().parent.parent
    if str(package_parent) not in sys.path:
        sys.path.insert(0, str(package_parent))
    spec_path, result_path = Path(argv[1]), Path(argv[2])
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _fail(result_path, "", "worker_error", f"spec 读取失败：{exc}")
        return
    run(spec, result_path)


if __name__ == "__main__":
    main(sys.argv)
