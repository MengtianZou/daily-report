"""看板账号鉴权工具：pbkdf2 密码哈希与会话 token（纯标准库）。"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_PBKDF2_ITERATIONS = 100_000


def hash_password(password: str) -> str:
    """生成 `salt$hash` 格式的密码哈希（每用户独立随机盐）。"""

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储的 `salt$hash` 是否匹配；格式非法返回 False。"""

    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    ).hex()
    return hmac.compare_digest(candidate, digest)


def new_session_token() -> str:
    """生成会话 token。"""

    return secrets.token_urlsafe(32)
