# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — 桌面客户端 (onedir 模式)。

为什么 onedir 而非 onefile:
  - onefile 每次启动都解压到临时 _MEIxxxxx, 与 APScheduler/多线程冲突
  - onedir 启动更快, 调试更方便 (可看到目录结构), 原生库直接在目录里
  - 体积差异通过压缩安装包弥补 (CI 里 zip 打包)

入口: backend/app/desktop.py (桌面版入口, 含 uvicorn + pywebview)

构建 (在项目根目录):
  cd frontend && pnpm build                     # 先构建前端到 frontend/dist
  pyinstaller packaging/tickflow.spec           # 产物在 dist/TickFlowStockPanel/
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
    collect_data_files,
    copy_metadata,
)

block_cipher = None

# ── 资源路径基准: 项目根 (spec 文件在 packaging/ 下) ──────────────────
ROOT = Path(SPECPATH).parent
FRONTEND_DIST = str(ROOT / "frontend" / "dist")
TIERS_YAML = str(ROOT / "tiers.yaml")
BUILTIN_STRATEGIES = str(ROOT / "backend" / "app" / "strategy" / "builtin")
APP_ICON = str(ROOT / "packaging" / "icon.ico")

# ── 收集带原生库的依赖 (.libs/ 目录必须完整, 否则启动崩) ─────────────
# polars / pyarrow / duckdb / fastexcel 都自带共享库子目录
datas = []
binaries = []
hiddenimports = []

for pkg in ("polars", "pyarrow", "duckdb", "fastexcel"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# polars 新 ABI 运行时目录 (_polars_runtime_32) 需显式收集子模块
hiddenimports += collect_submodules("polars")

# ── pywebview 平台后端 (动态导入, PyInstaller 默认抓不到) ────────────
hiddenimports += collect_submodules("webview")
hiddenimports += collect_submodules("webview.platforms")

# ── 系统通知后端 (winotify/plyer 按平台动态导入) ─────────────────────
if sys.platform == "win32":
    hiddenimports += collect_submodules("winotify")
hiddenimports += collect_submodules("plyer")
hiddenimports += collect_submodules("plyer.platforms")

# ── uvicorn 动态导入的模块 (loop/protocol/logging 按字符串加载) ──────
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# ── fastapi / pydantic 元数据 (版本检测用) ───────────────────────────
# 注意: 任何用 importlib.metadata.version() 读版本的包, 都必须 copy_metadata,
# 否则 frozen 后报 PackageNotFoundError。tickflow 包内部就是这么读的。
# 用容错写法: 不存在的包跳过, 避免不同环境 (有无装某依赖) 导致构建失败。
def _safe_metadata(pkg):
    """收集包元数据, 包不存在时静默跳过。"""
    try:
        return copy_metadata(pkg)
    except Exception:
        return []

for pkg in (
    "fastapi", "pydantic", "pydantic_settings", "starlette", "anyio",
    "tickflow",  # tickflow/__version__.py 用 importlib.metadata 读版本
    "uvicorn", "polars", "duckdb", "pyarrow", "httpx", "numpy", "pandas",
    "openai", "platformdirs", "winotify", "plyer", "apscheduler",
    "python-dotenv", "fastexcel",
):
    datas += _safe_metadata(pkg)

# ── 随包资源 (只读, 放进 _MEIPASS) ────────────────────────────────────
# 前端 dist → static/ (config.py frozen 模式读 _MEIPASS/static)
datas += [(FRONTEND_DIST, "static")]
# tiers.yaml → 包根 (config.py frozen 模式读 _MEIPASS/tiers.yaml)
datas += [(TIERS_YAML, ".")]
# 内置策略 → app/strategy/builtin/ (importlib 动态加载, 不能进 PYZ)
datas += [(BUILTIN_STRATEGIES, "app/strategy/builtin")]

# ── 排除不需要的重型依赖 (主包不含 vectorbt 回测链) ──────────────────
excludes = [
    "vectorbt",
    "numba",
    "llvmlite",
    "matplotlib",
    "plotly",
    "ipywidgets",
    "nbformat",
    "nbconvert",
    "jupyter",
    "IPython",
    "pytest",
    "pytest_asyncio",
    "ruff",
    "mypy",
]

a = Analysis(
    [str(ROOT / "backend" / "app" / "desktop.py")],
    pathex=[str(ROOT / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TickFlowStockPanel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX 压缩原生库常导致崩溃, 关闭
    console=False,       # 桌面应用: 不显示控制台窗口 (调试时临时改 True 抓日志)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,      # 应用图标 (与 favicon/logo 一致)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TickFlowStockPanel",
)
