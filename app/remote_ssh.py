"""SSH 远程执行（v0.25）：服务端用系统 ssh 集中操作编译服务器，密钥不下发 agent。

- password 模式：sshpass 注入密码；
- key 模式：解密后的私钥写入临时文件（600），ssh -i 引用，用完即删；
- 主机指纹用 accept-new，首次连接自动登记，避免交互卡住。
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class SshResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _base_args(host: str, port: int, username: str) -> list[str]:
    return [
        "ssh",
        "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "LogLevel=ERROR",
        f"{username}@{host}",
    ]


def run_remote(
    host: str,
    port: int,
    username: str,
    auth_mode: str,
    secret: str | None,
    remote_cmd: str,
    timeout: int = 120,
) -> SshResult:
    key_path: str | None = None
    args: list[str] = []
    try:
        if auth_mode == "agent":
            # 复用运行 TRES 服务的本机已配置免密（~/.ssh 默认密钥/agent/config），不存任何凭据
            args = [
                "ssh", "-p", str(port),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                "-o", "LogLevel=ERROR",
                f"{username}@{host}",
                remote_cmd,
            ]
        elif auth_mode == "key":
            if not secret:
                return SshResult(1, "", "缺少私钥")
            fd, key_path = tempfile.mkstemp(prefix="tres_key_", suffix=".pem")
            with os.fdopen(fd, "w") as f:
                f.write(secret)
            os.chmod(key_path, 0o600)
            args = [
                "ssh", "-p", str(port), "-i", key_path,
                "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                "-o", "LogLevel=ERROR",
                f"{username}@{host}",
                remote_cmd,
            ]
        else:
            if not secret:
                return SshResult(1, "", "缺少密码")
            args = [
                "sshpass", "-p", secret,
                *_base_args(host, port, username),
                "-o", "PreferredAuthentications=password,publickey",
                remote_cmd,
            ]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return SshResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    except subprocess.TimeoutExpired:
        return SshResult(124, "", f"超时（{timeout}s）")
    finally:
        if key_path:
            try:
                os.unlink(key_path)
            except OSError:
                pass
