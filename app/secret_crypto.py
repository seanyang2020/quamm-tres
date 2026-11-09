"""静态加密（v0.25）：编译服务器等敏感凭据的 Fernet 对称加密。

主密钥来源（二选一，主密钥不进数据库）：
1. 环境变量 QUAMM_MASTER_KEY：urlsafe base64 的 32 字节 Fernet key；
2. 否则在 app/var/master.key 自动生成（权限 600），仅本机服务可读。

加密只保证「数据库文件泄露时密文不可直接还原」；运行中的服务持有主密钥，
因此采用服务端集中执行、不把明文/密钥下发给 agent 的口径（见 remote_ssh / sdk_build）。
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_VAR_DIR = Path(__file__).resolve().parent / "var"
_KEY_FILE = _VAR_DIR / "master.key"


def _load_or_create_key() -> bytes:
    env = os.environ.get("QUAMM_MASTER_KEY")
    if env:
        return env.encode("utf-8")
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt_secret(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("无法解密：主密钥不匹配或密文已损坏") from exc


def mask_secret(plaintext: str | None) -> str:
    """接口回显用：只留末 4 位，其余掩码。"""
    if not plaintext:
        return ""
    tail = plaintext[-4:]
    return "••••" + tail
