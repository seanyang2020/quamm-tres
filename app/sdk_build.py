"""SDK 编译升级编排（v0.25）：服务端经 SSH 在编译机拉代码/编译，可再升级设备。

agent 只调高层 MCP 工具，全程不接触 SSH 密钥；工作目录 ~/<work_root>/<机型>-<IP尾号>。
"""
from __future__ import annotations

from app.domain.catalog import DomainError
from app.models import BuildServer, ProductType
from app.remote_ssh import run_remote
from app.secret_crypto import decrypt_secret


def _resolve(db, type_code: str) -> tuple[ProductType, BuildServer, str]:
    pt = db.get(ProductType, type_code)
    if not pt:
        raise DomainError("PRODUCT_NOT_FOUND", f"机型不存在：{type_code}")
    if not pt.build_server_id:
        raise DomainError("NO_BUILD_SERVER", f"机型 {type_code} 未配置编译服务器")
    bs = db.get(BuildServer, pt.build_server_id)
    if not bs:
        raise DomainError("BUILD_SERVER_NOT_FOUND", "引用的编译服务器不存在")
    try:
        secret = decrypt_secret(bs.secret_encrypted)
    except ValueError as exc:
        raise DomainError("MASTER_KEY_MISMATCH", str(exc))
    return pt, bs, secret


def run_sdk_build(db, *, type_code: str, ip_suffix: str | None = None,
                  timeout: int = 1800) -> dict:
    pt, bs, secret = _resolve(db, type_code)
    if not pt.sdk_repo_cmd or not pt.build_cmd:
        raise DomainError(
            "BUILD_CONFIG_INCOMPLETE",
            "机型缺少 sdk_repo_cmd / build_cmd，请在设备类型中补齐",
        )

    subdir = f"{type_code}-{ip_suffix}" if ip_suffix else type_code
    workdir = f"~/{bs.work_root}/{subdir}"
    steps: list[dict] = []

    def run(name: str, cmd: str, t: int) -> None:
        full = f"mkdir -p {workdir} && cd {workdir} && {{ {cmd}; }}"
        r = run_remote(bs.host, bs.port, bs.username, bs.auth_mode, secret, full, timeout=t)
        steps.append({"step": name, "rc": r.rc, "ok": r.ok,
                      "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]})

    run("prepare_repo", pt.sdk_repo_cmd, min(timeout, 600))
    if steps[-1]["ok"]:
        run("build", pt.build_cmd, timeout)
    else:
        steps.append({"step": "build", "rc": None, "ok": False,
                      "stdout": "", "stderr": "拉代码失败，已跳过编译"})

    return {
        "type_code": type_code,
        "build_server": bs.name,
        "workdir": workdir,
        "image_path": pt.image_path,
        "success": all(s["ok"] for s in steps),
        "steps": steps,
    }
