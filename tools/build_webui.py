"""Build the self-contained MaiBot custom page. No network/build dependencies."""

import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "webui/source"


def build() -> None:
    template = (SRC / "index.template.html").read_text(encoding="utf-8")
    css = (
        (SRC / "panel.css").read_text(encoding="utf-8")
        + "\n"
        + (SRC / "fish.css").read_text(encoding="utf-8")
    )

    manifest = {}
    for asset in sorted((SRC / "assets").iterdir()):
        raw = asset.read_bytes()
        mime = "image/svg+xml" if asset.suffix == ".svg" else "image/webp"
        uri = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
        key = "FISH_URI" if asset.suffix == ".svg" else asset.stem.upper()
        css = css.replace("{{" + key + "}}", uri)
        template = template.replace("{{" + key + "}}", uri)
        if asset.suffix == ".svg":
            template = template.replace("{{FISH_SVG}}", raw.decode("utf-8"))
        manifest[asset.name] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    # W-14（2026-11-03）：注入点从「panel.js 里恰好一行 freshnessTimer=setInterval」
    # 的字符串匹配改为显式 token。旧写法在 panel.js 改掉锚点字符串时 str.replace 会
    # 静默返回原文，构建成功但页面缺 Mascot/Bear/SectionFish，renderExtras 直接
    # ReferenceError——现在 token 缺失/重复/未注入都会让构建立刻失败。
    token = "/* LBM_EXTENSIONS */"
    panel_js = (SRC / "panel.js").read_text(encoding="utf-8")
    found = panel_js.count(token)
    assert found == 1, f"injection token {token} must appear exactly once in panel.js, found {found}"
    # 2026-11-14 主题对比批：layout_fix.js（布局层行为，依赖 fish.js 的
    # lbm:detail-open 与 .fish-panel DOM）与 theme.js（临时主题切换器，依赖
    # 前三者）依次追加在 maimai.js/fish.js 之后，共享 panel.js 闭包、additive。
    extensions = (
        (SRC / "maimai.js").read_text(encoding="utf-8")
        + "\n"
        + (SRC / "fish.js").read_text(encoding="utf-8")
        + "\n"
        + (SRC / "layout_fix.js").read_text(encoding="utf-8")
        + "\n"
        + (SRC / "theme.js").read_text(encoding="utf-8")
        + "\n"
    )
    js = panel_js.replace(token, extensions, 1)
    # 2026-11-14 主题对比批：皮肤 CSS 走独立 slot，注入模板 #host-theme 之后的
    # <style id="lbm-themes">——built {{CSS}} 在宿主样式之前，同特异性 :root 令牌
    # 会被宿主 c.styles 压过；themes.css 以 html:root[data-lbm-theme=X] 更高特异性
    # 作用域 + 位于 #host-theme 之后，忠实还原三版预览的级联位置。
    themes_css = (SRC / "themes.css").read_text(encoding="utf-8")
    output = (
        template.replace("{{CSS}}", css)
        .replace("{{THEMES_CSS}}", themes_css)
        .replace("{{JS}}", js)
    )
    assert not re.search(r"\{\{[A-Z_]+\}\}", output), "unresolved asset slot"
    assert len(output.encode("utf-8")) < 2 * 1024 * 1024, "host page limit exceeded"
    # W-14：注入生效断言——产物必须真的包含各段扩展的关键标识（旧锚点漏注入时
    # 这里会失败，而不是静默产出缺桌宠/详情层的页面）。
    # 用完整定义串而非前缀：`const Bear` 会被 `const BearX` 这类改名的子串骗过。
    # 2026-11-14 主题对比批：布局层行为与主题切换器同样以文件头标记断言注入生效。
    for marker in (
        "const Mascot=(()=>{",
        "const Bear=(()=>{",
        "const SectionFish=(()=>{",
        "/* LBM_LAYOUT_FIX */",
        "/* LBM_THEME_SWITCHER */",
    ):
        assert marker in output, f"injected extensions missing: {marker}"
    assert '<style id="lbm-themes">' in output, "themes style slot missing"

    # finding #2（2026-11-04）：显式固定产物换行符，消除平台 SHA 漂移。
    # 旧写法 write_text 默认 newline=None——Windows 产 CRLF、Linux 产 LF，内容相同
    # 但 raw SHA 不同，跨平台复建在「逐文件 SHA」部署流程里造成假不一致。
    # 源文件经 read_text 通用换行已归一为 LF（内嵌 SVG 经 read_bytes 注入，可能带
    # CRLF），这里先统一归一为 LF、再显式转 CRLF 落盘——沿用既有部署的 CRLF 字节约定，
    # 未改动区域逐字节不变；write_bytes 不做任何平台换行翻译，跨平台逐字节确定。
    normalized = output.replace("\r\n", "\n").replace("\r", "\n")
    (ROOT / "webui/index.html").write_bytes(normalized.replace("\n", "\r\n").encode("utf-8"))
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (SRC / "asset-manifest.json").write_bytes(manifest_text.replace("\n", "\r\n").encode("utf-8"))
    print(
        "Built webui/index.html:",
        len(output.encode("utf-8")),
        "bytes;",
        len(manifest),
        "verified assets",
    )


if __name__ == "__main__":
    build()
