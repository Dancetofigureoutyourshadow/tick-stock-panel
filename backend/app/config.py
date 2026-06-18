"""全局配置 — 从环境变量 / .env 读取。"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 = backend/ 的父目录
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # TickFlow
    tickflow_api_key: str = Field(default="", description="留空启用 free 模式")

    # AI
    ai_provider: str = "openai_compat"
    ai_base_url: str = "https://api.alysc.top"
    ai_api_key: str = ""
    ai_model: str = "gpt-5.5"
    ai_daily_token_budget: int = 5_000_000

    # Server
    host: str = "0.0.0.0"
    port: int = 3018
    log_level: str = "INFO"
    backtest_range_guard: bool = False

    # Data — 默认使用项目根目录的 data/，可通过 DATA_DIR 环境变量覆盖
    data_dir: Path = _PROJECT_ROOT / "data"

    # tiers.yaml 路径(项目根目录)
    tiers_yaml: Path = _PROJECT_ROOT / "tiers.yaml"

    # 静态文件(前端 dist) — 部署时只需 rsync 到 frontend/dist
    static_dir: Path = _PROJECT_ROOT / "frontend" / "dist"

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        """确保 data_dir 是绝对路径（环境变量传入的相对路径基于项目根目录解析）。"""
        if not self.data_dir.is_absolute():
            # 相对路径基于项目根目录解析，而非 CWD
            self.data_dir = (_PROJECT_ROOT / self.data_dir).resolve()
        return self

    @property
    def use_free_mode(self) -> bool:
        """是否走 Free 模式。优先看 secrets.json,其次看 .env。"""
        from app import secrets_store
        return not secrets_store.get_tickflow_key()


settings = Settings()
