"""预置数据：机型字典（5 类）+ 20 个 token（20 邮箱，每人 1 条）。

用法：python -m app.seed
输出：可复制的 MCP 客户端配置。
"""
from __future__ import annotations

from datetime import timedelta

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import Credential, Principal, ProductType
from app.utils import hash_token, new_token, token_prefix, utcnow

PRODUCT_TYPES = [
    # type_code, display_name, has_screen, has_led, serial_count, adb_port, model_pattern
    ("QM102V", "QM102V", True, True, 1, 5555, "QM102V"),
    ("QM10XD", "QM10XD", True, False, 2, 5555, "QM10XD"),
    ("QM108H", "QM108H", False, True, 1, 5555, "QM108H"),
    ("QM102V-Lite", "QM102V Lite", False, False, 1, 5555, "QM102V-Lite"),
    ("BES2800BP", "BES2800BP", False, False, 2, 5555, "BES2800BP"),
]


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


def seed_tokens(session, count: int = 20, admin_index: int = 0, rotate: bool = False) -> list[tuple[str, str]]:
    """每人 1 条 token，返回 (email, token) 列表。admin_index 指定的邮箱设为管理员。

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
                is_admin=(i - 1 == admin_index),
                status="active",
                created_at=now,
            )
            session.add(principal)
            session.flush()

        # 幂等：已存在未吊销的凭据则复用
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
                    created_at=now,
                )
            )
            result.append((email, token))
        session.commit()
    return result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="预置机型字典与 20 个 token")
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
        tokens = seed_tokens(session, count=20, rotate=args.rotate)
    finally:
        session.close()

    print("=== 机型字典 ===")
    for code, name, *_ in PRODUCT_TYPES:
        print(f"  {code}  ({name})")
    print()
    print("=== 20 个 token（每人 1 条，有效期 90 天）===")
    if not tokens:
        print("  已有可用凭据，未重新签发（如需明文请加 --rotate）")
    mcp_url = f"http://localhost:{settings.mcp_port}/mcp/"
    for email, token in tokens:
        print(f"  {email:<22} {token}")

    print()
    print("=== MCP 客户端配置示例（可直接粘贴）===")
    print('{')
    print('  "mcpServers": {')
    print('    "quamm-res": {')
    print(f'      "url": "{mcp_url}",')
    print('      "headers": {')
    if tokens:
        print(f'        "Authorization": "Bearer {tokens[0][1]}"')
    print('      }')
    print('    }')
    print('  }')
    print('}')
    print()
    print(f"（也可用 URL 参数形式：{mcp_url}?t=rr_... ）")


if __name__ == "__main__":
    main()
