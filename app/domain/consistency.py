"""设备工程配置一致性检查（v0.31）。

数据源：Device.eng_config（探测时写入的 build-info + config.gz 关键项）。

判定维度：
1. 指纹态 state：fingerprinted（有 build-info）/ legacy（能连但老固件）/ unknown
2. 机型-配置 verdict：ProductType.defconfig（期望）vs build_info.gz_filename（实际）
3. 机型-实况（弱检测）：model_class vs 探测到的 project_name，legacy 设备也适用
4. 重复登记：同 ADB 地址、或同 config_sha256 出现多个 device_id

原则：只打标，不改变设备可用性（不阻断申领、不改变 health/status）。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import Device, ProductType


def _load_eng_config(d: Device) -> dict:
    raw = getattr(d, "eng_config", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def defconfig_short(name: str | None) -> str | None:
    """project_framex_defconfig -> framex（前端展示用短名）"""
    if not name:
        return None
    n = name
    if n.startswith("project_"):
        n = n[len("project_"):]
    if n.endswith("_defconfig"):
        n = n[: -len("_defconfig")]
    return n or None


def _other_devices(session: Session, d: Device):
    """同池其它设备。Device.adb_address 是 property，不能直接进 query，取 ip+port 拼。"""
    return (
        session.query(Device.device_id, Device.ip, Device.adb_port, Device.eng_config)
        .filter(Device.device_id != d.device_id, Device.status != "retired")
        .all()
    )


def check_device(session: Session, d: Device) -> dict[str, Any]:
    """返回单台设备的配置检查结果（给 API 与前端消费）。"""
    payload = _load_eng_config(d)
    bi = payload.get("build_info") or {}
    eng = payload.get("eng_config") or {}

    expected = None
    if d.model_class:
        pt = session.get(ProductType, d.model_class)
        expected = getattr(pt, "defconfig", None) if pt is not None else None

    defconfig = bi.get("gz_filename") or None
    sha256 = bi.get("config_sha256") or None

    if bi or defconfig:
        state = "fingerprinted"
    elif d.health_level in ("L2", "L3"):
        # 当前可达但不能读 build-info：老固件
        state = "legacy"
    elif d.last_seen_at is not None:
        # 曾经可达、现在离线：仍按老固件记录，但离线不计入在线覆盖率
        state = "legacy"
    else:
        state = "unknown"

    issues: list[str] = []
    verdict = "unknown"

    # ---- 重复登记：同 IP / 同指纹 ----
    dup_ids: list[str] = []
    my_ip = (d.ip or "").strip()
    my_port = d.adb_port or 5555
    for other_id, other_ip, other_port, other_cfg in _other_devices(session, d):
        if my_ip and other_ip and other_ip.strip() == my_ip and (other_port or 5555) == my_port:
            dup_ids.append(other_id)
            continue
        if sha256 and other_cfg:
            try:
                ocfg = json.loads(other_cfg)
            except (ValueError, TypeError):
                continue
            if isinstance(ocfg, dict) and (ocfg.get("build_info") or {}).get("config_sha256") == sha256:
                dup_ids.append(other_id)
    if dup_ids:
        issues.append("重复登记：" + "、".join(sorted(set(dup_ids))))
        verdict = "duplicate"

    # ---- 机型-配置一致性（强检测，需 build-info）----
    if state == "fingerprinted" and expected and defconfig:
        if expected != defconfig:
            issues.append("配置不符：期望 %s，实际 %s" % (expected, defconfig))
            if verdict in ("unknown", "ok"):
                verdict = "mismatch_defconfig"
        elif verdict == "unknown":
            verdict = "ok"

    # ---- 机型-实况一致性（弱检测，legacy 也适用）----
    if d.project_name and d.model_class and d.project_name != d.model_class:
        # 只有 project_name 是已知机型代码时才认定为矛盾，避免把别名误报
        if session.get(ProductType, d.project_name) is not None:
            issues.append("机型不符：登记 %s，实况 %s" % (d.model_class, d.project_name))
            if verdict in ("unknown", "ok"):
                verdict = "mismatch_model"

    return {
        "state": state,
        "defconfig": defconfig,
        "defconfig_short": defconfig_short(defconfig),
        "sha8": (sha256 or "")[:8] or None,
        "git": bi.get("project_git") or None,
        "built_at": bi.get("built_at") or None,
        "host": bi.get("host") or None,
        "product_name": d.project_name,
        "expected_defconfig": expected,
        "expected_short": defconfig_short(expected),
        "verdict": verdict,
        "issues": issues,
    }


def audit_pool(session: Session) -> dict[str, Any]:
    """全池配置体检：覆盖率、机型-配置错配、重复登记、指纹分布。

    只读，不修改任何设备状态。
    """
    devices = (
        session.query(Device).filter(Device.status != "retired").all()
    )
    rows = []
    for d in devices:
        c = check_device(session, d)
        rows.append(
            {
                "device_id": d.device_id,
                "display_name": d.display_name,
                "model_class": d.model_class,
                "position_code": d.position_code,
                "health_level": d.health_level,
                "status": d.status,
                "state": c["state"],
                "defconfig": c["defconfig"],
                "defconfig_short": c["defconfig_short"],
                "sha8": c["sha8"],
                "git": c["git"],
                "expected_defconfig": c["expected_defconfig"],
                "verdict": c["verdict"],
                "issues": c["issues"],
            }
        )

    total = len(rows)
    online = len([r for r in rows if r["health_level"] in ("L2", "L3")])
    fp = [r for r in rows if r["state"] == "fingerprinted"]
    legacy = [r for r in rows if r["state"] == "legacy"]
    unknown = [r for r in rows if r["state"] == "unknown"]

    # 重复登记：按 IP 与按指纹分组
    by_ip: dict[str, list[str]] = {}
    by_sha: dict[str, list[str]] = {}
    for d in devices:
        ip = (d.ip or "").strip()
        if ip:
            by_ip.setdefault(ip, []).append(d.device_id)
    for r in fp:
        if r["sha8"]:
            by_sha.setdefault(r["sha8"], []).append(r["device_id"])

    dup_groups: list[list[str]] = []
    seen_key: set[str] = set()
    for group in list(by_ip.values()) + list(by_sha.values()):
        if len(group) <= 1:
            continue
        key = "|".join(sorted(group))
        if key in seen_key:
            continue
        seen_key.add(key)
        dup_groups.append(sorted(group))

    def _pct(n: int, base: int) -> float:
        return round(100.0 * n / base, 1) if base else 0.0

    mismatches = [r for r in rows if r["verdict"] in ("mismatch_defconfig", "mismatch_model")]

    by_defconfig: dict[str, list[str]] = {}
    for r in fp:
        if r["defconfig"]:
            by_defconfig.setdefault(r["defconfig"], []).append(r["sha8"] or "?")

    git_count: dict[str, int] = {}
    for r in fp:
        g = r["git"] or "unknown"
        git_count[g] = git_count.get(g, 0) + 1

    return {
        "total": total,
        "online": online,
        "fingerprinted": len(fp),
        "legacy": len(legacy),
        "unknown": len(unknown),
        "coverage": {
            "vs_total_pct": _pct(len(fp), total),
            "vs_online_pct": _pct(len(fp), online),
        },
        "verdicts": {
            v: len([r for r in rows if r["verdict"] == v])
            for v in ("ok", "duplicate", "mismatch_defconfig", "mismatch_model", "unknown")
        },
        "duplicate_groups": dup_groups,
        "mismatches": mismatches,
        "by_defconfig": {
            k: {"count": len(v), "shas": sorted(set(v))}
            for k, v in sorted(by_defconfig.items())
        },
        "git_versions": dict(sorted(git_count.items(), key=lambda kv: -kv[1])),
        "devices": rows,
    }
