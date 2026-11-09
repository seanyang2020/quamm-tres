"""编译服务器领域（v0.25）：CRUD + 加密保管 + 添加时自动探测决定能力。

约定：db 首参，写库末尾显式 commit；序列化只回显掩码，绝不返回明文/密文。
"""
from __future__ import annotations

import secrets as _secrets

from app.domain.catalog import DomainError
from app.models import BuildServer, ProductType
from app.remote_ssh import run_remote, tcp_reachable
from app.secret_crypto import decrypt_secret, encrypt_secret, mask_secret
from app.utils import utcnow


def _new_id() -> str:
    return "bs_" + _secrets.token_hex(6)


def generate_ssh_keypair() -> tuple[str, str]:
    """生成 ed25519 密钥对，返回 (OpenSSH 私钥 PEM, OpenSSH 公钥)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_ssh = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private_pem, public_ssh


def create_build_server(db, *, actor: str, name: str, host: str, port: int = 22,
                        username: str, auth_mode: str = "password",
                        secret: str | None = None, work_root: str = "tres-auto") -> dict:
    name, host, username = name.strip(), host.strip(), username.strip()
    if not name or not host or not username:
        raise DomainError("VALIDATION_ERROR", "名称/主机/用户名不能为空")
    if auth_mode not in ("password", "key"):
        raise DomainError("VALIDATION_ERROR", "auth_mode 仅支持 password/key")
    if db.query(BuildServer).filter(BuildServer.name == name).first():
        raise DomainError("BUILD_SERVER_NAME_TAKEN", f"名称已存在：{name}")

    public_key = None
    if auth_mode == "key" and not secret:
        secret, public_key = generate_ssh_keypair()

    row = BuildServer(
        server_id=_new_id(), name=name, host=host, port=int(port or 22),
        username=username, auth_mode=auth_mode,
        secret_encrypted=encrypt_secret(secret),
        public_key=public_key, work_root=work_root or "tres-auto",
        status="unknown", created_at=utcnow(),
    )
    db.add(row)
    db.add(_audit(actor, "build_server_create", row.server_id, {"name": name, "host": host}))
    db.commit()
    db.refresh(row)
    return serialize(row)


def update_build_server(db, *, actor: str, server_id: str,
                        name: str | None = None, host: str | None = None,
                        port: int | None = None, username: str | None = None,
                        auth_mode: str | None = None, secret: str | None = None,
                        work_root: str | None = None) -> dict:
    row = db.get(BuildServer, server_id)
    if not row:
        raise DomainError("BUILD_SERVER_NOT_FOUND", f"编译服务器不存在：{server_id}")

    if name is not None and name.strip():
        other = db.query(BuildServer).filter(
            BuildServer.name == name.strip(), BuildServer.server_id != server_id
        ).first()
        if other:
            raise DomainError("BUILD_SERVER_NAME_TAKEN", f"名称已存在：{name}")
        row.name = name.strip()
    if host is not None and host.strip():
        row.host = host.strip()
    if port is not None:
        row.port = int(port)
    if username is not None and username.strip():
        row.username = username.strip()
    if work_root is not None and work_root.strip():
        row.work_root = work_root.strip()
    if auth_mode is not None:
        if auth_mode not in ("password", "key"):
            raise DomainError("VALIDATION_ERROR", "auth_mode 仅支持 password/key")
        row.auth_mode = auth_mode
    # 仅当显式传入非空 secret 才重新加密；留空表示沿用原密钥
    if secret:
        row.secret_encrypted = encrypt_secret(secret)
        row.public_key = None
    row.updated_at = utcnow()
    db.add(_audit(actor, "build_server_update", row.server_id, {"name": row.name}))
    db.commit()
    db.refresh(row)
    return serialize(row)


def delete_build_server(db, *, actor: str, server_id: str) -> dict:
    row = db.get(BuildServer, server_id)
    if not row:
        raise DomainError("BUILD_SERVER_NOT_FOUND", f"编译服务器不存在：{server_id}")
    refs = db.query(ProductType).filter(ProductType.build_server_id == server_id).count()
    if refs:
        raise DomainError("BUILD_SERVER_IN_USE", f"仍被 {refs} 个机型引用，请先解除引用", {"refs": refs})
    db.add(_audit(actor, "build_server_delete", row.server_id, {"name": row.name}))
    db.delete(row)
    db.commit()
    return {"deleted": server_id}


def list_build_servers(db) -> list[dict]:
    return [serialize(r) for r in db.query(BuildServer).order_by(BuildServer.created_at).all()]


def get_build_server(db, server_id: str) -> dict:
    row = db.get(BuildServer, server_id)
    if not row:
        raise DomainError("BUILD_SERVER_NOT_FOUND", f"编译服务器不存在：{server_id}")
    return serialize(row)


def probe_build_server(db, *, server_id: str, subdir: str | None = None) -> dict:
    row = db.get(BuildServer, server_id)
    if not row:
        raise DomainError("BUILD_SERVER_NOT_FOUND", f"编译服务器不存在：{server_id}")
    try:
        plain = decrypt_secret(row.secret_encrypted)
    except ValueError as exc:
        raise DomainError("MASTER_KEY_MISMATCH", str(exc))

    detail: dict = {"steps": []}

    def step(name: str, ok: bool, info: str = "") -> None:
        detail["steps"].append({"step": name, "ok": ok, "info": info})

    # 1. TCP
    reachable = tcp_reachable(row.host, row.port)
    step("tcp", reachable, f"{row.host}:{row.port}")
    if not reachable:
        return _finish(db, row, detail, "unavailable")

    # 2. 认证
    auth = run_remote(row.host, row.port, row.username, row.auth_mode, plain,
                      "echo AUTH_OK; whoami; pwd", timeout=30)
    step("auth", auth.ok, (auth.stdout or auth.stderr)[:200])
    if not auth.ok:
        return _finish(db, row, detail, "unavailable")

    # 3. 建工作目录
    target = row.work_root + ("/" + subdir if subdir else "")
    mkdir = run_remote(row.host, row.port, row.username, row.auth_mode, plain,
                       f"mkdir -p ~/{target} && cd ~/{target} && pwd", timeout=30)
    step("workdir", mkdir.ok, (mkdir.stdout or mkdir.stderr)[:200])

    # 4. git 可用性
    git = run_remote(row.host, row.port, row.username, row.auth_mode, plain,
                     "git --version || echo NO_GIT", timeout=30)
    step("git", git.ok and "NO_GIT" not in git.stdout, (git.stdout or git.stderr)[:120])

    # 5. 工具链（best-effort，不致命）
    tool = run_remote(row.host, row.port, row.username, row.auth_mode, plain,
                      "for t in make gcc ssh; do command -v $t >/dev/null && echo have:$t; done",
                      timeout=30)
    step("toolchain", tool.ok, (tool.stdout or tool.stderr)[:200])

    all_ok = all(s["ok"] for s in detail["steps"] if s["step"] in ("tcp", "auth", "workdir", "git"))
    return _finish(db, row, detail, "available" if all_ok else "unavailable")


def _finish(db, row: BuildServer, detail: dict, status: str) -> dict:
    row.probe_detail = detail
    row.status = status
    row.last_probe_at = utcnow()
    row.updated_at = utcnow()
    db.commit()
    db.refresh(row)
    return serialize(row)


def serialize(row: BuildServer) -> dict:
    plain = None
    if row.secret_encrypted:
        try:
            plain = decrypt_secret(row.secret_encrypted)
        except ValueError:
            plain = None
    return {
        "server_id": row.server_id,
        "name": row.name,
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "auth_mode": row.auth_mode,
        "secret_mask": mask_secret(plain),
        "has_secret": bool(row.secret_encrypted),
        "public_key": row.public_key,
        "work_root": row.work_root,
        "status": row.status,
        "probe_detail": row.probe_detail,
        "last_probe_at": row.last_probe_at.isoformat() if row.last_probe_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _audit(actor: str, action: str, server_id: str, detail: dict):
    from app.models import AuditEvent
    return AuditEvent(actor=actor, action=action, device_id=None, lease_id=None,
                      detail={"server_id": server_id, **detail}, created_at=utcnow())
