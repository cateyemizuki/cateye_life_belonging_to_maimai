"""Run every isolated regression suite against this checkout or CATEYE_TEST_DIR."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    tests = Path(__file__).resolve().parent
    source = Path(os.environ.get("CATEYE_TEST_DIR") or tests.parent).resolve()
    if not (source / "plugin.py").is_file():
        print(f"Plugin checkout not found at {source}", file=sys.stderr)
        return 2
    missing = [name for name in ("pydantic", "httpx", "PIL") if importlib.util.find_spec(name) is None]
    if missing:
        print("Missing test dependencies: " + ", ".join(missing) + ". See tests/README.md.", file=sys.stderr)
        return 2
    if shutil.which("node") is None:
        print("Node.js is required for the production WebUI contract tests.", file=sys.stderr)
        return 2
    env = {
        **os.environ,
        "CATEYE_TEST_DIR": str(source),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    failed = []
    suites = sorted(tests.glob("test_astra_*.py"))
    for suite in suites:
        print(f"\nRunning {suite.name}", flush=True)
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(suite)], cwd=source, env=env)
        if result.returncode:
            failed.append(suite.name)
    if failed:
        print("\nFailed suites: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"\nAll {len(suites)} suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
