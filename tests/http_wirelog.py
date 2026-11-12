"""WireLog HTTP 集成：TestClient 走 enroll/register/heartbeat/nodes + 鉴权拦截。"""
import os, tempfile
db = tempfile.mktemp(suffix=".db")
os.environ["RES_DATABASE_URL"] = f"sqlite:///{db}"

from app.db import configure, SessionLocal, init_db
configure(f"sqlite:///{db}")
init_db()

from app.seed import seed_admin, ADMIN_EMAIL
from app.models import Credential, Device
from app.utils import utcnow
from app import web_auth

s = SessionLocal()
seed_admin(s)
s.add(Device(device_id="dev-dut1", owner="t", status="online",
             mac="AA:BB:CC:11:22:33", created_at=utcnow()))
s.commit()
cred_id = s.query(Credential).first().id
s.close()

cookie = web_auth.make_session(cred_id)

from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
fails = []
def check(name, cond, extra=""):
    print("PASS" if cond else "FAIL", name, extra)
    if not cond: fails.append(name)

# 无 cookie 管理接口 401
check("未登录 nodes 401", c.get("/api/wirelog/nodes").status_code == 401)
# 无 Bearer 注册 401
check("无 token 注册 401",
      c.post("/api/wirelog/register", json={}).status_code == 401)

# 管理员发放
r = c.post("/api/wirelog/enroll",
           json={"adb_address": "172.16.60.122:5555"},
           cookies={web_auth.SESSION_COOKIE: cookie})
check("enroll 200", r.status_code == 200, r.text)
en = r.json()
token, node_id = en["node_token"], en["node_id"]

# 节点注册（Bearer）
r = c.post("/api/wirelog/register",
           headers={"Authorization": f"Bearer {token}"},
           json={"ip": "172.16.60.122", "sw_version": "1.0",
                 "ports": [{"slot": 1, "tty": "/dev/ttyUSB0", "baud": 115200,
                            "shell_reachable": True,
                            "shell_mac": "aa:bb:cc:11:22:33",
                            "shell_ip": "172.16.60.99"}]})
check("register 200", r.status_code == 200, r.text)
check("注册回写策略", "1" in r.json()["write_policy"])

# 坏 token 注册 401
r = c.post("/api/wirelog/register",
           headers={"Authorization": "Bearer bad"}, json={})
check("坏 token 401", r.status_code == 401)

# 心跳
r = c.post("/api/wirelog/heartbeat",
           headers={"Authorization": f"Bearer {token}"}, json={"ports": []})
check("heartbeat 200", r.status_code == 200)

# 列表/详情（cookie）
r = c.get("/api/wirelog/nodes",
          cookies={web_auth.SESSION_COOKIE: cookie})
check("nodes 200", r.status_code == 200 and len(r.json()) == 1)
r = c.get(f"/api/wirelog/nodes/{node_id}",
          cookies={web_auth.SESSION_COOKIE: cookie})
info = r.json()
check("详情自动绑定", info["ports"][0]["bound_device_id"] == "dev-dut1")
check("详情串口就绪", info["ports"][0]["shell_reachable"] is True)

# 写授权
r = c.post(f"/api/wirelog/nodes/{node_id}/write-grant",
           json={"slot": 1, "principal": "p7"},
           cookies={web_auth.SESSION_COOKIE: cookie})
check("写授权 200", r.status_code == 200 and
      r.json()["1"]["write_granted_to"] == "p7")
r = c.post(f"/api/wirelog/nodes/{node_id}/write-grant",
           json={"slot": 1, "revoke": True},
           cookies={web_auth.SESSION_COOKIE: cookie})
check("撤销 200", r.json()["1"]["write_granted_to"] is None)

print("RESULT", "ALL_PASS" if not fails else f"FAIL={fails}")
