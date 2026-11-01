"""Sampling geometry and bounded montage packing; no SDK or model credentials."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from pathlib import Path

TILES_PER_IMAGE = 16
MAX_IMAGE_BYTES = 256 * 1024


def sample_plan(duration: float, interval: int = 25, max_montages: int = 16) -> dict:
    if not math.isfinite(float(duration)) or duration <= 0 or interval <= 0 or max_montages <= 0:
        raise ValueError("视频时长或采样配置无效")
    count = max(1, math.ceil(duration / interval))
    images = math.ceil(count / TILES_PER_IMAGE)
    if images > max_montages:
        raise ValueError(f"按每{interval}秒采样需{count}帧、{images}张拼图，超过{max_montages}张上限；可明确调大采样间隔或拼图上限")
    return {"frame_count": count, "montage_count": images,
            "timestamps": [round(duration * (i + .5) / count, 3) for i in range(count)]}


def capability(spec: dict) -> str:
    params = {k: spec.get(k) for k in ("source_mode", "sampling_mode", "interval_seconds", "max_montages")}
    return "montage-v3-" + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10]


def pack_montages(paths: list[Path], timestamps: list[float]) -> list[dict]:
    from PIL import Image, ImageDraw, ImageOps
    packed = []
    for start in range(0, len(paths), TILES_PER_IMAGE):
        batch = paths[start:start + TILES_PER_IMAGE]
        rows = math.ceil(len(batch) / 4)
        image = Image.new("RGB", (1280, rows * 202), "#ffffff")
        draw = ImageDraw.Draw(image)
        for n, path in enumerate(batch):
            x, y = (n % 4) * 320, (n // 4) * 202
            with Image.open(path) as source:
                tile = ImageOps.contain(source.convert("RGB"), (320, 180))
                image.paste(tile, (x + (320-tile.width)//2, y))
            at = timestamps[start + n]
            draw.text((x + 4, y + 183), f"{int(at)//3600:02d}:{int(at)%3600//60:02d}:{at%60:05.2f}", fill="#000000")
        buffer = io.BytesIO();image.save(buffer, format="JPEG", quality=75)
        raw = buffer.getvalue()
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("单张拼图超出传输保护范围；未发送超大图片")
        packed.append({"image_format": "jpeg", "image_base64": base64.b64encode(raw).decode("ascii"),
                       "sampled_seconds": timestamps[start:start+len(batch)], "tile_count": len(batch)})
    return packed
