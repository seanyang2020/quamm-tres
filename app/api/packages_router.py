"""客户端安装包分发 API（t976）。

- GET    /api/packages                 登录可读
- POST   /api/packages                 仅 admin（multipart）
- GET    /api/packages/{id}/download   登录即可
- DELETE /api/packages/{id}            仅 admin
- PUT    /api/packages/{id}/channel    仅 admin（可选）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import web_auth
from app.db import get_session
from app.domain import packages as pkg_svc
from app.domain.catalog import DomainError

router = APIRouter(prefix="/packages", tags=["packages"])


def _raise_domain(e: DomainError) -> None:
    code = e.code or ""
    if code == "PACKAGE_TOO_LARGE":
        status = 413
    elif code == "PACKAGE_DUP":
        status = 409
    elif code in ("PACKAGE_NOT_FOUND", "PACKAGE_FILE_MISSING"):
        status = 404
    elif code in (
        "PACKAGE_BAD_FILENAME",
        "PACKAGE_BAD_EXT",
        "PACKAGE_BAD_META",
        "PACKAGE_BAD_PLATFORM",
        "PACKAGE_BAD_CHANNEL",
        "PACKAGE_EMPTY",
    ):
        status = 400
    else:
        status = 400
    raise HTTPException(
        status_code=status,
        detail={"code": e.code, "message": e.message, "detail": e.detail},
    )


@router.get("")
def list_packages(
    session: Session = Depends(get_session),
    _identity=Depends(web_auth.require_login),
):
    return {"items": pkg_svc.list_packages(session)}


@router.post("")
async def upload_package(
    file: UploadFile = File(...),
    name: str = Form(...),
    version: str = Form(...),
    platform: str = Form(...),
    channel: str = Form("prod"),
    notes: str | None = Form(None),
    session: Session = Depends(get_session),
    identity=Depends(web_auth.require_admin),
):
    try:
        return pkg_svc.upload_package(
            session,
            file_obj=file.file,
            filename=file.filename or "",
            name=name,
            version=version,
            platform=platform,
            channel=channel,
            notes=notes,
            actor=identity.email,
        )
    except DomainError as e:
        _raise_domain(e)
    finally:
        await file.close()


@router.get("/{pkg_id}/download")
def download_package(
    pkg_id: int,
    session: Session = Depends(get_session),
    identity=Depends(web_auth.require_login),
):
    try:
        pkg = pkg_svc.get_package(session, pkg_id)
        path = pkg_svc.mark_downloaded(session, pkg, actor=identity.email)
    except DomainError as e:
        _raise_domain(e)
    return FileResponse(
        path=str(path),
        filename=pkg.filename,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
    )


@router.delete("/{pkg_id}")
def delete_package(
    pkg_id: int,
    session: Session = Depends(get_session),
    identity=Depends(web_auth.require_admin),
):
    try:
        return pkg_svc.delete_package(session, pkg_id, actor=identity.email)
    except DomainError as e:
        _raise_domain(e)


@router.put("/{pkg_id}/channel")
def set_channel(
    pkg_id: int,
    payload: dict,
    session: Session = Depends(get_session),
    identity=Depends(web_auth.require_admin),
):
    channel = (payload or {}).get("channel", "")
    try:
        return pkg_svc.set_channel(
            session, pkg_id, channel, actor=identity.email
        )
    except DomainError as e:
        _raise_domain(e)
