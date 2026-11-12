"""WireLog 后端冒烟：建表 + 发放/注册/心跳/绑定/授权 全链路。"""
import os, tempfile

db = tempfile.mktemp(suffix=".db")
os.environ["RES_DATABASE_URL"] = f"sqlite:///{db}"

from app.db import configure, SessionLocal, init_db
configure(f"sqlite:///{db}")
init_db()

from app.models import Device
from app.domain import wirelog
from app.utils import utcnow

s = SessionLocal()
# 预置一个被测设备（带 MAC）
s.add(Device(device_id="dev-dut1", owner="t", status="online",
             mac="AA:BB:CC:11:22:33", created_at=utcnow()))
s.commit()

fails = []
def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond: fails.append(name)

# 发放
en = wirelog.enroll(s, adb_address="172.16.60.122:5555", operator="tester")
check("enroll 返回一次性 token", en["node_token"] and en["fw_setenv"])
node_id, token = en["node_id"], en["node_token"]

# 错误 token 被拒
try:
    wirelog._auth_node(s, "bad"); check("坏 token 拒绝", False)
except Exception: check("坏 token 拒绝", True)

# 注册
reg = wirelog.register(s, token, {
    "ip": "172.16.60.122", "sw_version": "1.0.0",
    "caps": {"ports": 1},
    "ports": [{"slot": 1, "tty": "/dev/ttyUSB0", "baud": 115200,
               "shell_reachable": True, "shell_mac": "aa:bb:cc:11:22:33",
               "shell_ip": "172.16.60.99"}]})
check("注册回 node_id", reg["node_id"] == node_id)
check("心跳间隔 60", reg["heartbeat_interval_s"] == 60)

# 自动 MAC 绑定
info = wirelog.get_node(s, node_id)
b1 = info["ports"][0]
check("MAC 自动绑定", b1["bound_device_id"] == "dev-dut1", b1)
check("串口就绪态", b1["shell_reachable"] is True)

# 心跳
hb = wirelog.heartbeat(s, token, {"ip": "172.16.60.122", "ports": []})
check("心跳回写策略", "1" in hb["write_policy"])

# 写授权
pol = wirelog.grant_write(s, node_id, 1, "principal-7")
check("写授权生效", pol["1"]["write_granted_to"] == "principal-7")
pol = wirelog.revoke_write(s, node_id, 1)
check("撤销写授权", pol["1"]["write_granted_to"] is None)

# 配置下发
cfg = wirelog.set_config(s, node_id, {"raw_tcp_enable": 1})
check("配置下发", cfg["config"]["raw_tcp_enable"] == 1)

# 列表/审计
check("节点列表", any(n["node_id"] == node_id for n in wirelog.list_nodes(s)))
check("审计有记录", len(wirelog.audit_tail(s, node_id)) >= 4)

# 状态刷新
wirelog.refresh_status(s)
print("RESULT", "ALL_PASS" if not fails else f"FAIL={fails}")
