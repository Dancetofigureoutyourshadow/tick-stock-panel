#!/usr/bin/env python3
"""版本号自增脚本 — 由 git prepare-commit-msg hook 调用。

读取当前版本, +0.0.1, 写回 frontend/package.json 和 backend/pyproject.toml。
跳过条件(避免 merge/rebase/amend 误触发):
  - 非 master/main 分支(可选, 当前不限制)
  - commit message 以 merge/squash/rebase 开头
  - 环境变量 SKIP_VERSION_BUMP=1
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "frontend" / "package.json"
PYPROJECT = ROOT / "backend" / "pyproject.toml"
APP_INIT = ROOT / "backend" / "app" / "__init__.py"  # __version__ 写在这里, health 接口读它


def parse_version(v: str) -> tuple[int, int, int]:
    parts = v.strip().split(".")
    while len(parts) < 3:
        parts.append("0")
    return int(parts[0]), int(parts[1]), int(parts[2])


def bump(v: str) -> str:
    major, minor, patch = parse_version(v)
    return f"{major}.{minor}.{patch + 1}"


def read_pkg_version() -> str:
    data = json.loads(PKG.read_text(encoding="utf-8"))
    return data["version"]


def write_pkg_version(v: str) -> None:
    data = json.loads(PKG.read_text(encoding="utf-8"))
    data["version"] = v
    PKG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def write_pyproject_version(v: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    text = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{v}"', text, count=1, flags=re.MULTILINE)
    PYPROJECT.write_text(text, encoding="utf-8")


def write_app_init_version(v: str) -> None:
    """更新 backend/app/__init__.py 的 __version__ (health 接口读这里)。"""
    text = APP_INIT.read_text(encoding="utf-8")
    text = re.sub(
        r'^(__version__\s*=\s*)"[^"]+"',
        rf'\1"{v}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    APP_INIT.write_text(text, encoding="utf-8")


def main() -> int:
    # 环境变量跳过
    if __import__("os").environ.get("SKIP_VERSION_BUMP") == "1":
        return 0

    cur = read_pkg_version()
    new = bump(cur)

    # 三个文件版本可能不一致, 统一用 pkg 的为准
    write_pkg_version(new)
    write_pyproject_version(new)
    write_app_init_version(new)

    # 暂存版本文件改动(并入本次 commit)
    import subprocess
    subprocess.run(["git", "add", str(PKG), str(PYPROJECT), str(APP_INIT)], check=True, cwd=str(ROOT))

    # 输出新版本号供 hook 读用
    print(f"[bump] {cur} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
