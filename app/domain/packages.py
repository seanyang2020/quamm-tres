"""Standalone（deskserialhub / ELSB）客户端安装包：上传 / 列表 / 下载 / 删除。

存储：``<data_dir>/packages/<id>-<filename>``，写入用临时文件 + ``os.replace`` 原子落盘。
鉴权由路由层 ``web_auth`` 负责；本模块只做校验、落盘与审计。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.domain import audit
from app.domain.catalog import DomainError
from app.models import WlPackage
from app.utils import utcnow

_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_ALLOWED_EXT = {".zip", ".exe", ".msi"}
_ALLOWED_CHANNELS = {"prod", "beta"}
_ALLOWED_PLATFORMS = {"Windows", "Linux"}


def packages_dir() -> Path:
    root = Path(settings.data_dir).expanduser().resolve()
    d = root / "packages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_filename(raw: str) -> str:
    """清洗上传文件名：仅保留 ``[A-Za-z0-9._+-]``，禁用路径分隔符。"""
    if not raw or not isinstance(raw, str):
        raise DomainError("PACKAGE_BAD_FILENAME", "文件名无效")
    # 拒绝路径穿越 / 分隔符；不以 basename 静默吞掉（避免掩盖攻击意图）
    if "/" in raw or "\\" in raw or ".." in raw or raw != Path(raw).name:
        raise DomainError("PACKAGE_BAD_FILENAME", "文件名含非法路径字符")
    if not _SAFE_FILENAME_RE.fullmatch(raw):
        raise DomainError(
            "PACKAGE_BAD_FILENAME",
            "文件名仅允许字母数字与 ._+-，请重命名后上传",
        )
    ext = Path(raw).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise DomainError(
            "PACKAGE_BAD_EXT",
            f"仅支持 {', '.join(sorted(_ALLOWED_EXT))} 扩展名",
        )
    return raw


def storage_path(pkg: WlPackage) -> Path:
    return packages_dir() / f"{pkg.id}-{pkg.filename}"


def _to_dict(pkg: WlPackage) -> dict:
    return {
        "id": pkg.id,
        "name": pkg.name,
        "version": pkg.version,
        "platform": pkg.platform,
        "filename": pkg.filename,
        "size_bytes": pkg.size_bytes,
        "sha256": pkg.sha256,
        "notes": pkg.notes,
        "channel": pkg.channel,
        "uploaded_by": pkg.uploaded_by,
        "uploaded_at": pkg.uploaded_at.isoformat() + "Z" if pkg.uploaded_at else None,
        "download_count": pkg.download_count,
        "download_url": f"/api/packages/{pkg.id}/download",
    }


def list_packages(session: Session) -> list[dict]:
    rows = (
        session.query(WlPackage)
        .order_by(WlPackage.channel.asc(), WlPackage.uploaded_at.desc())
        .all()
    )
    return [_to_dict(r) for r in rows]


def upload_package(
    session: Session,
    *,
    file_obj,
    filename: str,
    name: str,
    version: str,
    platform: str,
    channel: str = "prod",
    notes: str | None = None,
    actor: str,
    content_length: int | None = None,
) -> dict:
    """multipart 上传：校验扩展名/大小，原子落盘，算 sha256，写审计。

    同名同版本同平台已存在 → DomainError PACKAGE_DUP（路由映射 409）。
    超限 → PACKAGE_TOO_LARGE（413）。
    """
    safe_name = sanitize_filename(filename)
    name = (name or "").strip()
    version = (version or "").strip()
    platform = (platform or "").strip()
    channel = (channel or "prod").strip().lower()
    if not name or not version:
        raise DomainError("PACKAGE_BAD_META", "name / version 必填")
    if platform not in _ALLOWED_PLATFORMS:
        raise DomainError(
            "PACKAGE_BAD_PLATFORM",
            f"platform 仅支持 {', '.join(sorted(_ALLOWED_PLATFORMS))}",
        )
    if channel not in _ALLOWED_CHANNELS:
        raise DomainError("PACKAGE_BAD_CHANNEL", "channel 仅支持 prod|beta")

    max_bytes = int(settings.package_max_bytes)
    if content_length is not None and content_length > max_bytes:
        raise DomainError(
            "PACKAGE_TOO_LARGE",
            f"安装包超过上限 {max_bytes} 字节",
            detail={"max_bytes": max_bytes, "content_length": content_length},
        )

    dup = (
        session.query(WlPackage)
        .filter_by(name=name, version=version, platform=platform)
        .first()
    )
    if dup is not None:
        raise DomainError(
            "PACKAGE_DUP",
            f"已存在 {name} {version}（{platform}），请换版本号后再上传",
            detail={"id": dup.id},
        )

    # 先占行拿到 id，再落盘；失败则回滚删行
    now = utcnow()
    pkg = WlPackage(
        name=name,
        version=version,
        platform=platform,
        filename=safe_name,
        size_bytes=0,
        sha256="",
        notes=(notes or None),
        channel=channel,
        uploaded_by=actor,
        uploaded_at=now,
        download_count=0,
    )
    session.add(pkg)
    session.flush()  # 分配 id

    dest = storage_path(pkg)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{pkg.id}-", suffix=".part", dir=str(packages_dir())
    )
    os.close(tmp_fd)
    h = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise DomainError(
                        "PACKAGE_TOO_LARGE",
                        f"安装包超过上限 {max_bytes} 字节",
                        detail={"max_bytes": max_bytes, "size": size},
                    )
                h.update(chunk)
                out.write(chunk)
        if size <= 0:
            raise DomainError("PACKAGE_EMPTY", "空文件不可上传")
        os.replace(tmp_path, dest)
        tmp_path = None  # 已原子替换，勿再删
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        session.delete(pkg)
        session.commit()
        raise

    pkg.size_bytes = size
    pkg.sha256 = h.hexdigest()
    audit.record(
        session,
        actor=actor,
        action="comres_package_upload",
        detail={
            "id": pkg.id,
            "name": name,
            "version": version,
            "platform": platform,
            "channel": channel,
            "filename": safe_name,
            "size_bytes": size,
            "sha256": pkg.sha256,
        },
    )
    session.commit()
    session.refresh(pkg)
    return _to_dict(pkg)


def get_package(session: Session, pkg_id: int) -> WlPackage:
    pkg = session.get(WlPackage, pkg_id)
    if pkg is None:
        raise DomainError("PACKAGE_NOT_FOUND", f"安装包 {pkg_id} 不存在")
    return pkg


def mark_downloaded(session: Session, pkg: WlPackage, *, actor: str) -> Path:
    path = storage_path(pkg)
    if not path.is_file():
        raise DomainError("PACKAGE_FILE_MISSING", "安装包文件缺失，请重新上传")
    pkg.download_count = int(pkg.download_count or 0) + 1
    audit.record(
        session,
        actor=actor,
        action="comres_package_download",
        detail={
            "id": pkg.id,
            "name": pkg.name,
            "version": pkg.version,
            "platform": pkg.platform,
            "filename": pkg.filename,
            "download_count": pkg.download_count,
            "actor_email": actor,
        },
    )
    session.commit()
    return path


def delete_package(session: Session, pkg_id: int, *, actor: str) -> dict:
    pkg = get_package(session, pkg_id)
    path = storage_path(pkg)
    info = {
        "id": pkg.id,
        "name": pkg.name,
        "version": pkg.version,
        "platform": pkg.platform,
        "filename": pkg.filename,
    }
    session.delete(pkg)
    audit.record(
        session,
        actor=actor,
        action="comres_package_delete",
        detail=info,
    )
    session.commit()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
    return {"ok": True, **info}


def set_channel(session: Session, pkg_id: int, channel: str, *, actor: str) -> dict:
    channel = (channel or "").strip().lower()
    if channel not in _ALLOWED_CHANNELS:
        raise DomainError("PACKAGE_BAD_CHANNEL", "channel 仅支持 prod|beta")
    pkg = get_package(session, pkg_id)
    old = pkg.channel
    pkg.channel = channel
    audit.record(
        session,
        actor=actor,
        action="comres_package_channel",
        detail={"id": pkg.id, "from": old, "to": channel},
    )
    session.commit()
    session.refresh(pkg)
    return _to_dict(pkg)
