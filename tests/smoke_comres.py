"""COMRes 冒烟：迁移字段、双 node 同 ip:port、解注册、SN/MAC/IP 匹配、续约超时释放。"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

db = tempfile.mktemp(suffix=".db")
os.environ["RES_DATABASE_URL"] = f"sqlite:///{db}"

from app.db import configure, SessionLocal, init_db
configure(f"sqlite:///{db}")
init_db()

from sqlalchemy import inspect, text
from app.db import SessionLocal as SL
from app.migrations import migrate
from app.models import Device, WireLogPortBinding
from app.domain import comres, wirelog
from app.utils import utcnow

s = SessionLocal()
migrate(s)

fails = []


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra if extra != "" else "")
    if not cond:
        fails.append(name)


# ---------- 1) 迁移：新字段 + 唯一索引 ----------
eng = s.get_bind()
cols = {c["name"] for c in inspect(eng).get_columns("wl_port_binding")}
need = {"tcp_port", "com_key", "allocated_by", "allocated_at", "allocated_expire"}
print("=== ACCEPT-1 migration columns ===")
print("columns", sorted(need & cols))
idxs = {i["name"]: i for i in inspect(eng).get_indexes("wl_port_binding")}
print("indexes", sorted(idxs))
check("迁移含 COMRes 列", need <= cols, sorted(need - cols))
check("唯一索引 uq_wl_comres_key", "uq_wl_comres_key" in idxs)

# ---------- 预置设备 ----------
now = utcnow()
s.add(Device(device_id="dev-sn", owner="t", status="online",
             sn="SN-HIT-001", created_at=now))
s.add(Device(device_id="dev-mac", owner="t", status="online",
             mac="AA:BB:CC:DD:EE:01", created_at=now))
s.add(Device(device_id="dev-ip", owner="t", status="online",
             ip="10.0.0.77", created_at=now))
s.commit()

# ---------- 两个节点 ----------
n1 = wirelog.provision_host(s, display_name="desk-A", operator="tester")
n2 = wirelog.provision_host(s, display_name="desk-B", operator="tester")
token1, id1 = n1["node_token"], n1["node_id"]
token2, id2 = n2["node_token"], n2["node_id"]

# ---------- 2) 两 node 同 ip:port 互不覆盖 ----------
ep = "tcp://10.0.0.5:9200"
r1 = comres.register(s, token1, {
    "node_id": id1, "local_ip": "10.0.0.5", "sw_version": "elsb-test",
    "ports": [{"slot": 1, "com": "COM11", "endpoint": ep,
               "health_state": "online", "baud": 115200}],
})
r2 = comres.register(s, token2, {
    "node_id": id2, "local_ip": "10.0.0.5", "sw_version": "elsb-test",
    "ports": [{"slot": 1, "com": "COM11", "endpoint": ep,
               "health_state": "online", "baud": 115200}],
})
rows = (s.query(WireLogPortBinding)
        .filter(WireLogPortBinding.com_key == "10.0.0.5:9200").all())
print("=== ACCEPT-2 dual node same ip:port ===")
for b in rows:
    print(f"  node={b.node_id} com_key={b.com_key} endpoint={b.endpoint} slot={b.slot}")
check("两行互不覆盖", len(rows) == 2, f"count={len(rows)}")
check("register 回 allocated 字段", "allocated" in r1["ports"][0])
check("heartbeat_interval 3600", r1.get("heartbeat_interval_s") == 3600)

# node1 再加第二端口，供解注册保留对照
comres.register(s, token1, {
    "node_id": id1, "local_ip": "10.0.0.5",
    "ports": [
        {"slot": 1, "com": "COM11", "endpoint": ep, "health_state": "online"},
        {"slot": 2, "com": "COM12", "endpoint": "tcp://10.0.0.5:9201",
         "health_state": "online"},
    ],
})
before = (s.query(WireLogPortBinding).filter_by(node_id=id1).all())
print("=== ACCEPT-3 unregister before ===")
for b in before:
    print(f"  slot={b.slot} com={b.com_name} key={b.com_key}")
check("解注册前 2 口", len(before) == 2)

un = comres.unregister(s, token1, {
    "node_id": id1, "local_ip": "10.0.0.5",
    "ports": [{"com": "COM11", "endpoint": ep}],
})
after = (s.query(WireLogPortBinding).filter_by(node_id=id1).all())
print("=== ACCEPT-3 unregister after ===")
print("removed", un["removed"])
for b in after:
    print(f"  slot={b.slot} com={b.com_name} key={b.com_key}")
check("撤销后剩 1 口", len(after) == 1 and after[0].com_name == "COM12")

# ---------- 4) 自动匹配四种 ----------
def match_case(label, port_extra, expect_device, expect_basis):
    # 每用例独立节点，避免污染
    prov = wirelog.provision_host(s, display_name=f"m-{label}", operator="tester")
    tok, nid = prov["node_token"], prov["node_id"]
    port = {
        "slot": 1, "com": f"COM-{label}", "baud": 115200,
        "endpoint": f"tcp://10.9.9.{ord(label[0]) % 200}:9200",
        "health_state": "online",
        **port_extra,
    }
    # IP 命中用例：endpoint/local_ip 对齐 device.ip
    local_ip = port_extra.get("_local_ip", "10.9.9.1")
    if "_local_ip" in port_extra:
        del port["_local_ip"]
    resp = comres.register(s, tok, {
        "node_id": nid, "local_ip": local_ip, "ports": [port],
    })
    b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
    print(f"=== ACCEPT-4 {label} ===")
    print(f"  allocated={resp['ports'][0]['allocated']} by={resp['ports'][0]['allocated_by']} "
          f"device={b.bound_device_id} com_key={b.com_key}")
    ok_dev = (b.bound_device_id == expect_device) if expect_device else (b.bound_device_id is None)
    ok_alloc = resp["ports"][0]["allocated"] is bool(expect_device)
    # 审计依据
    from app.models import WireLogAudit
    aud = (s.query(WireLogAudit).filter_by(node_id=nid, action="comres_match")
           .order_by(WireLogAudit.id.desc()).first())
    import json
    detail = json.loads(aud.detail) if aud and aud.detail else {}
    print(f"  audit_basis={detail.get('basis')} result={aud.result if aud else None}")
    check(f"match-{label}-device", ok_dev, f"got={b.bound_device_id}")
    check(f"match-{label}-basis", detail.get("basis") == expect_basis, detail)
    check(f"match-{label}-allocated", ok_alloc)
    return nid, tok, b


match_case("sn", {"sn": "SN-HIT-001"}, "dev-sn", "sn")
match_case("mac", {"mac": "aa:bb:cc:dd:ee:01"}, "dev-mac", "mac")
match_case("ip", {"_local_ip": "10.0.0.77",
                  "endpoint": "tcp://10.0.0.77:9200"}, "dev-ip", "ip")
match_case("miss", {"sn": None, "mac": None}, None, "miss")

# ---------- 5) 占用续约 + 超时释放 ----------
prov = wirelog.provision_host(s, display_name="lease-n", operator="tester")
tok, nid = prov["node_token"], prov["node_id"]
reg = comres.register(s, tok, {
    "node_id": nid, "local_ip": "10.0.0.88",
    "ports": [{"slot": 1, "com": "COM99", "sn": "SN-HIT-001",
               "endpoint": "tcp://10.0.0.88:9200", "health_state": "online"}],
})
# SN 命中后已 allocated；手动缩短 TTL 再续约
b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
check("续约前已分配", reg["ports"][0]["allocated"] is True, reg["ports"][0])
hb = comres.allocated_heartbeat(s, {
    "node_id": nid, "com": "COM99", "endpoint": "tcp://10.0.0.88:9200",
    "allocated_by": "tester-lease", "ttl_s": 60,
})
print("=== ACCEPT-5 renew ===")
print(hb)
check("续约成功", hb["ok"] and hb["allocated"] and hb["allocated_by"] == "tester-lease")
b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
# 人为过期
b.allocated_expire = utcnow() - timedelta(seconds=1)
s.commit()
released = comres.expire_allocations(s)
print("=== ACCEPT-5 expired release ===")
print("released", released)
b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
print(f"  after: allocated_by={b.allocated_by} device={b.bound_device_id} expire={b.allocated_expire}")
check("超时释放回池", b.allocated_by is None and b.bound_device_id is None)
check("释放记审计", any(r.get("com") == "COM99" for r in released) or len(released) >= 1)

# ---------- 6) resources 两组 ----------
# 再造一条 allocated + 一条 pool
prov_a = wirelog.provision_host(s, display_name="res-a", operator="t")
comres.register(s, prov_a["node_token"], {
    "node_id": prov_a["node_id"], "local_ip": "10.1.1.1",
    "ports": [{"slot": 1, "com": "COM-A", "sn": "SN-HIT-001",
               "endpoint": "tcp://10.1.1.1:9200", "health_state": "online"}],
})
prov_p = wirelog.provision_host(s, display_name="res-p", operator="t")
comres.register(s, prov_p["node_token"], {
    "node_id": prov_p["node_id"], "local_ip": "10.1.1.2",
    "ports": [{"slot": 1, "com": "COM-P",
               "endpoint": "tcp://10.1.1.2:9200", "health_state": "online"}],
})
res = comres.list_resources(s)
print("=== ACCEPT-6 resources ===")
print(f"allocated={len(res['allocated'])} pool={len(res['pool'])}")
if res["allocated"]:
    print("  alloc sample", {k: res["allocated"][0].get(k)
                             for k in ("node_id", "com", "endpoint", "health_state",
                                       "allocated", "device_id")})
if res["pool"]:
    print("  pool sample", {k: res["pool"][0].get(k)
                            for k in ("node_id", "com", "endpoint", "health_state",
                                      "allocated", "device_id")})
check("已分配带 device_id", all(x.get("device_id") for x in res["allocated"]))
check("资源池不带 device_id", all(x.get("device_id") is None for x in res["pool"]))
check("字段含 endpoint/com/allocated",
      all(all(k in x for k in ("endpoint", "node_id", "com", "health_state", "allocated"))
          for x in res["allocated"] + res["pool"]))

# ---------- 7) 旧 wirelog 接口域仍可用 ----------
nodes = wirelog.list_nodes(s)
print("=== ACCEPT-7 wirelog nodes ===")
print(f"list_nodes count={len(nodes)} sample_id={nodes[0]['node_id'] if nodes else None}")
check("wirelog.list_nodes 可用", len(nodes) >= 1)

# FastAPI 路由挂载检查（OpenAPI；新版 routes 为 _IncludedRouter 不扁平）
from app.main import app
oa_paths = set(app.openapi().get("paths", {}))
check("路由 /api/comres/register", "/api/comres/register" in oa_paths)
check("路由 /api/wirelog/nodes", "/api/wirelog/nodes" in oa_paths)
print("=== ACCEPT-7 routes ===")
print("comres", sorted(p for p in oa_paths if "/comres" in p))
print("wirelog/nodes", "/api/wirelog/nodes" in oa_paths)

# HTTP 级：旧 /api/wirelog/nodes 需登录 → 无 cookie 应 401/403，证明路由仍在
from fastapi.testclient import TestClient
client = TestClient(app)
resp = client.get("/api/wirelog/nodes")
print("GET /api/wirelog/nodes status", resp.status_code)
check("旧 wirelog/nodes 仍响应", resp.status_code in (401, 403, 200))
resp2 = client.post("/api/comres/register", json={})
print("POST /api/comres/register (no auth) status", resp2.status_code)
check("comres/register 无 token→401", resp2.status_code == 401)

print("RESULT", "ALL_PASS" if not fails else f"FAIL={fails}")
raise SystemExit(0 if not fails else 1)
