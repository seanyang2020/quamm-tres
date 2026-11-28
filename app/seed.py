"""预置数据：机型字典（5 类）+ 管理员 + 20 个 token + 人员通讯录种子。

用法：python -m app.seed
输出：可复制的 MCP 客户端配置（含管理员 admin@corp.com 的 token）。
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from app.config import settings
from app.constants import get_version
from app.db import SessionLocal, init_db
from app.models import Contact, Credential, Principal, ProductType
from app.utils import hash_token, new_token, token_prefix, utcnow

ADMIN_EMAIL = "admin@corp.com"
CONTACTS_SEED_PATH = "/tmp/contacts_seed.json"

# v0.17：移除旧 demo/mock 机型种子，避免每次启动 seed_product_types 把它们重新插回。
# 真实机型由设备探测/设备类型管理页维护，不再预置 mock 机型。
PRODUCT_TYPES: list[tuple] = []


def seed_product_types(session) -> None:
    for code, name, has_screen, has_led, serial_count, adb_port, pattern in PRODUCT_TYPES:
        if session.get(ProductType, code) is None:
            session.add(
                ProductType(
                    type_code=code,
                    display_name=name,
                    has_screen=has_screen,
                    has_led=has_led,
                    serial_count=serial_count,
                    adb_port=adb_port,
                    enabled=True,
                    model_pattern=pattern,
                    created_at=utcnow(),
                )
            )
    session.commit()


def _ensure_credential(session, principal: Principal, now, rotate: bool) -> str | None:
    """幂等：复用未吊销凭据；rotate 时吊销旧的并签发新 token。返回新 token 明文（若签发）。"""
    existing = (
        session.query(Credential)
        .filter(Credential.principal_id == principal.id, Credential.revoked_at.is_(None))
        .first()
    )
    if existing is not None and rotate:
        existing.revoked_at = now
        existing = None
    if existing is None:
        token = new_token()
        session.add(
            Credential(
                principal_id=principal.id,
                token_hash=hash_token(token),
                token_prefix=token_prefix(token),
                expires_at=now + timedelta(days=settings.credential_valid_days),
                role="admin" if principal.is_admin else "user",
                created_at=now,
            )
        )
        return token
    return None


def seed_tokens(session, count: int = 20, rotate: bool = False) -> list[tuple[str, str]]:
    """每人 1 条普通 token（is_admin=False），返回 (email, token) 列表。

    rotate=True：吊销已有未吊销凭据并签发新凭据（演示脚本需要明文 token 时使用；
    token 只存哈希，历史明文无法反查）。
    """
    result = []
    now = utcnow()
    for i in range(1, count + 1):
        email = f"user{i:02d}@corp.com"
        principal = session.query(Principal).filter(Principal.email == email).first()
        if principal is None:
            principal = Principal(
                email=email,
                name=f"测试用户{i:02d}",
                employee_no=f"E{i:03d}",
                quota=settings.holder_concurrency_limit,
                is_admin=False,
                status="active",
                created_at=now,
            )
            session.add(principal)
            session.flush()
        else:
            principal.is_admin = False  # 幂等修正历史误标的管理员

        token = _ensure_credential(session, principal, now, rotate)
        if token is not None:
            result.append((email, token))
        session.commit()
    return result


def seed_admin(session, rotate: bool = False) -> tuple[str, str] | None:
    """确保管理员 principal 存在（admin@corp.com，is_admin=True），并签发/复用凭据。

    同时把其余所有人的 is_admin 置 False（修正历史误标，保证 admin 门有效）。
    返回 (email, token)（仅当本次新签发明文时）。
    """
    now = utcnow()
    principal = session.query(Principal).filter(Principal.email == ADMIN_EMAIL).first()
    if principal is None:
        principal = Principal(
            email=ADMIN_EMAIL,
            name="管理员",
            employee_no="E000",
            quota=0,  # 管理员不占配额，靠 is_admin 走管理动作
            is_admin=True,
            status="active",
            created_at=now,
        )
        session.add(principal)
        session.flush()
    else:
        principal.is_admin = True

    # 除 admin@corp.com 外一律降级为普通账号
    session.query(Principal).filter(Principal.email != ADMIN_EMAIL).update(
        {Principal.is_admin: False}, synchronize_session=False
    )

    token = _ensure_credential(session, principal, now, rotate)
    session.commit()
    if token is not None:
        return (ADMIN_EMAIL, token)
    return None


def seed_contacts(session, path: str = CONTACTS_SEED_PATH) -> int:
    """从 JSON 导入人员通讯录（{email: name}），幂等：已存在不覆盖、不报错。"""
    p = Path(path)
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    now = utcnow()
    added = 0
    for raw_email, name in data.items():
        email = str(raw_email or "").strip().lower()
        if not email or session.get(Contact, email) is not None:
            continue
        session.add(
            Contact(
                email=email,
                name=str(name) if name else None,
                note=None,
                created_at=now,
            )
        )
        added += 1
    if added:
        session.commit()
    return added


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="预置机型字典、管理员与 20 个 token")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="吊销已有凭据并重新签发（token 只存哈希，需要明文时使用）",
    )
    args = parser.parse_args()

    init_db()
    session = SessionLocal()
    try:
        seed_product_types(session)
        admin_pair = seed_admin(session, rotate=args.rotate)
        tokens = seed_tokens(session, count=20, rotate=args.rotate)
    finally:
        session.close()

    print("=== 机型字典 ===")
    for code, name, *_ in PRODUCT_TYPES:
        print(f"  {code}  ({name})")
    print()
    print(f"=== 管理员（{get_version()}）===")
    if admin_pair:
        print(f"  {admin_pair[0]:<22} {admin_pair[1]}")
    else:
        print(f"  {ADMIN_EMAIL:<22}（已有凭据，未重新签发；需明文请加 --rotate）")
    print()
    print("=== 20 个普通 token（每人 1 条，有效期 90 天，is_admin=False）===")
    if not tokens:
        print("  已有可用凭据，未重新签发（如需明文请加 --rotate）")
    mcp_url = f"http://localhost:{settings.mcp_port}/mcp/"
    for email, token in tokens:
        print(f"  {email:<22} {token}")

    print()
    print("=== MCP 客户端配置示例（可直接粘贴）===")
    print('{')
    print('  "mcpServers": {')
    print('    "quamm-tres": {')
    print(f'      "url": "{mcp_url}",')
    print('      "headers": {')
    if admin_pair:
        print(f'        "Authorization": "Bearer {admin_pair[1]}"')
    print('      }')
    print('    }')
    print('  }')
    print('}')
    print()
    print(f"（也可用 URL 参数形式：{mcp_url}?t=rr_... ）")


if __name__ == "__main__":
    main()
