"""SMTP 配置与发信（v0.11）：配置存 settings 表，可在线修改 + 发测试邮件 + 发接入凭据。

所有 SMTP 网络操作都带 15s 超时，避免挂起。纯文本邮件正文 = MCP 配置 JSON + 使用说明。
"""
from __future__ import annotations

import json
import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Setting
from app.utils import utcnow

SMTP_DEFAULTS: dict[str, str] = {
    "server": "smtp.exmail.qq.com",
    "port": "465",
    "user": "yangxiongwei@quaming.com",
    "password": "ap4GnmEokHb73s9j",
    "use_ssl": "true",
    "from": "yangxiongwei@quaming.com",
}
SMTP_KEYS = ("server", "port", "user", "password", "use_ssl", "from")
SMTP_TIMEOUT = 15  # 秒；所有 socket 操作限时

CREDENTIAL_SUBJECT = "【Quamm TRES】你的设备申请凭据"


def _as_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def get_smtp_config(session: Session) -> dict[str, str]:
    cfg = dict(SMTP_DEFAULTS)
    for row in session.query(Setting).filter(Setting.key.like("smtp.%")).all():
        cfg[row.key[len("smtp.") :]] = row.value or ""
    return cfg


def save_smtp_config(session: Session, payload: dict) -> dict[str, str]:
    for key in SMTP_KEYS:
        if key not in payload:
            continue
        value = str(payload[key])
        row = session.get(Setting, f"smtp.{key}")
        if row is None:
            session.add(Setting(key=f"smtp.{key}", value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()
    session.commit()
    return get_smtp_config(session)


def build_credential_body(token: str, note: str | None = None) -> str:
    mcp_config = {
        "mcpServers": {
            "quamm-tres": {
                "url": settings.mcp_public_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    lines = [
        "你好，这是 Quamm TRES（测试资源分配管理系统）的接入凭据。",
        "将以下 MCP 配置填入你的 AI Agent 即可申请 ADB 测试设备：",
        "",
        json.dumps(mcp_config, ensure_ascii=False, indent=2),
        "",
        "使用：Agent 调用 res_list_products / res_apply / res_release 即可申请释放设备。",
        "本 token 有效期 90 天，到期前管理员会续期。",
    ]
    if note:
        lines += ["", "备注：" + note]
    return "\n".join(lines)


def send_mail(
    session: Session,
    to: str,
    subject: str,
    body: str,
    cfg: dict[str, str] | None = None,
) -> None:
    cfg = cfg or get_smtp_config(session)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from") or cfg.get("user") or SMTP_DEFAULTS["user"]
    msg["To"] = to
    msg.set_content(body)

    server = cfg.get("server") or SMTP_DEFAULTS["server"]
    port = int(cfg.get("port") or SMTP_DEFAULTS["port"])
    user = cfg.get("user") or ""
    password = cfg.get("password") or ""

    if _as_bool(cfg.get("use_ssl", "true")):
        with smtplib.SMTP_SSL(server, port, timeout=SMTP_TIMEOUT) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(server, port, timeout=SMTP_TIMEOUT) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPException:
                pass
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
