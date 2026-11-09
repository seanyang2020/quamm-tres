"""pytest 公共 fixture：临时 SQLite + 预置机型/20 token + mock 探测。"""
from __future__ import annotations

import pytest

from app import models  # noqa: F401
from app.db import configure, get_session_factory, init_db
from app.domain.probe import MockProber, set_prober
from app.domain import devicelock
from app.models import Credential, Principal
from app.seed import seed_admin, seed_product_types, seed_tokens
from app.security import AuthContext

# 单元测试不跑真实 adb 物理闸门（无设备，避免超时拖慢/卡死）
devicelock.physical_gate_enabled = False


class Db:
    def __init__(self, url: str):
        configure(url)
        init_db()
        self.factory = get_session_factory()
        s = self.factory()
        try:
            seed_product_types(s)
            self.tokens = seed_tokens(s, count=20)
            admin_pair = seed_admin(s)
            if admin_pair is not None:
                self.tokens.append(admin_pair)
        finally:
            s.close()

    def session(self):
        return self.factory()

    def auth(self, email: str) -> AuthContext:
        s = self.factory()
        try:
            p = s.query(Principal).filter(Principal.email == email).first()
            c = s.query(Credential).filter(Credential.principal_id == p.id).first()
            return AuthContext(principal=p, credential=c, email=p.email, is_admin=p.is_admin)
        finally:
            s.close()

    def token(self, email: str) -> str:
        for e, t in self.tokens:
            if e == email:
                return t
        raise KeyError(email)

    def call(self, email: str, fn, **kwargs):
        """以某人的身份直接调用 MCP 工具函数（真实接口层，跳过 HTTP JSON-RPC）。"""
        from app.security import set_auth

        with set_auth(self.auth(email)):
            return fn(**kwargs)


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path}/test.db"
    return Db(url)


def register_device(db: Db, email: str, dev, position_code: str, owner: str) -> dict:
    """通过真实接口（res_device_probe -> res_device_register）注册一台模拟设备。"""
    from app.mcp.server import res_device_probe, res_device_register

    probe = db.call(email, res_device_probe, ip=dev.ip, port=dev.port)
    return db.call(
        email,
        res_device_register,
        form_id=probe["form_id"],
        position_code=position_code,
        owner=owner,
    )


@pytest.fixture(autouse=True)
def mock_prober():
    """测试统一用模拟设备 HTTP 探测。"""
    set_prober(MockProber())
    yield
