"""访问密码认证 — 单用户, 自托管场景。

设计:
  - 密码用 PBKDF2-HMAC-SHA256 哈希(标准库 hashlib, 无新依赖), 加随机 salt。
    即使 auth.json 泄露, 也无法逆向出明文密码。
  - 会话用随机 token(token_urlsafe), 内存 + 文件双存(支持多进程/重启不丢失)。
  - 存储: data/user_data/auth.json (chmod 0600), 仿 secrets_store 模式。

安全要点:
  - 设密码接口必须限制本机/内网(见 auth router), 防黑客抢占域名抢先设密码。
  - 登录限流: 错5次锁5分钟(见 auth router 内存计数)。
  - 单密码, 不做多用户(避免重构全项目数据层)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as _secrets
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# PBKDF2 参数(NIST 推荐, 单次校验 ~100ms, 兼顾安全与响应)
_PBKDF2_ITER = 200_000
_SALT_LEN = 16
_TOKEN_BYTES = 32

# 会话有效期: 30 天(自托管单用户, 长一点减少重登频率)
SESSION_TTL = 30 * 24 * 3600

_lock = threading.Lock()
# 内存中的有效会话: { token: expire_ts }。进程重启后从磁盘恢复。
_sessions: dict[str, float] = {}


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("auth.json malformed: %s", e)
    return {}


def _save(data: dict) -> None:
    p = _path()
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。salt 为 None 时生成新 salt。"""
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """恒定时间比较, 防时序攻击。"""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return _secrets.compare_digest(actual, expected)


# ================================================================
# 密码管理
# ================================================================

def is_configured() -> bool:
    """是否已设置访问密码。"""
    d = _load()
    return bool(d.get("password_hash"))


def set_password(password: str) -> None:
    """设置/修改访问密码。清空所有现有会话(强制重新登录)。"""
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt_hex, hash_hex = _hash_password(password)
    with _lock:
        _sessions.clear()  # 改密码 = 旧会话全部失效
        _save({
            "password_hash": hash_hex,
            "password_salt": salt_hex,
            "updated_at": int(time.time()),
            "sessions": {},  # 清空持久化会话
        })
    logger.info("access password set")


def verify_and_create_session(password: str) -> str | None:
    """验证密码, 成功则创建会话并返回 token, 失败返回 None。"""
    d = _load()
    if not d.get("password_hash"):
        return None
    if not _verify_password(password, d.get("password_salt", ""), d["password_hash"]):
        return None
    token = _secrets.token_urlsafe(_TOKEN_BYTES)
    expire = time.time() + SESSION_TTL
    with _lock:
        _sessions[token] = expire
        _persist_sessions_locked()
    return token


def revoke_session(token: str) -> None:
    """注销会话(登出)。"""
    with _lock:
        _sessions.pop(token, None)
        _persist_sessions_locked()


def is_valid_session(token: str) -> bool:
    """检查会话是否有效(存在且未过期)。过期则清理。"""
    if not token:
        return False
    with _lock:
        expire = _sessions.get(token)
        if expire is None:
            return False
        if time.time() > expire:
            _sessions.pop(token, None)
            _persist_sessions_locked()
            return False
        return True


def _persist_sessions_locked() -> None:
    """把当前内存会话写回 auth.json(需持锁调用)。"""
    d = _load()
    d["sessions"] = {t: exp for t, exp in _sessions.items()}
    _save(d)


def _restore_sessions() -> None:
    """启动时从 auth.json 恢复未过期会话(支持进程重启不丢登录态)。"""
    with _lock:
        d = _load()
        now = time.time()
        saved = d.get("sessions") or {}
        for token, expire in saved.items():
            if isinstance(expire, (int, float)) and expire > now:
                _sessions[token] = expire
        if len(_sessions) != len(saved):
            # 有过期会话被清理, 落盘一次
            _persist_sessions_locked()


# 模块加载时恢复会话
try:
    _restore_sessions()
except Exception as e:  # noqa: BLE001
    logger.warning("restore sessions failed: %s", e)
