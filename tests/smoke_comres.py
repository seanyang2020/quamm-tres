"""COMRes 冒烟：迁移、匹配、续约、自动建档、冲突进池、三态资源视图。"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta

db = tempfile.mktemp(suffix=".db")
os.environ["RES_DATABASE_URL"] = f"sqlite:///{db}"

from app.db import configure, SessionLocal, init_db
configure(f"sqlite:///{db}")
init_db()

from sqlalchemy import inspect
from app.migrations import migrate
from app.models import Device, WireLogAudit, WireLogPortBinding
from app.domain import comres, wirelog
from app.constants import RegisterChannel
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
def match_case(label, port_extra, expect_device, expect_basis, expect_action="comres_match"):
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
    aud = (s.query(WireLogAudit).filter_by(node_id=nid, action=expect_action)
           .order_by(WireLogAudit.id.desc()).first())
    detail = json.loads(aud.detail) if aud and aud.detail else {}
    print(f"  audit_action={expect_action} basis={detail.get('basis')} "
          f"result={aud.result if aud else None}")
    check(f"match-{label}-device", ok_dev, f"got={b.bound_device_id}")
    check(f"match-{label}-basis", detail.get("basis") == expect_basis, detail)
    check(f"match-{label}-allocated", ok_alloc)
    return nid, tok, b


# sn/mac 命中已有设备 → comres_device_register；纯 IP / miss → comres_match
match_case("sn", {"sn": "SN-HIT-001"}, "dev-sn", "sn",
           expect_action="comres_device_register")
match_case("mac", {"mac": "aa:bb:cc:dd:ee:01"}, "dev-mac", "mac",
           expect_action="comres_device_register")
match_case("ip", {"_local_ip": "10.0.0.77",
                  "endpoint": "tcp://10.0.0.77:9200"}, "dev-ip", "ip",
           expect_action="comres_match")
match_case("miss", {"sn": None, "mac": None}, None, "miss",
           expect_action="comres_match")

# ---------- 5) 占用续约 + 超时解除占用（保留绑定） ----------
prov = wirelog.provision_host(s, display_name="lease-n", operator="tester")
tok, nid = prov["node_token"], prov["node_id"]
reg = comres.register(s, tok, {
    "node_id": nid, "local_ip": "10.0.0.88",
    "ports": [{"slot": 1, "com": "COM99", "sn": "SN-HIT-001",
               "endpoint": "tcp://10.0.0.88:9200", "health_state": "online"}],
})
b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
# SN-HIT-001 可能已被 ACCEPT-4 sn 节点绑定 → conflict；清其它绑定后重试
if not reg["ports"][0]["allocated"]:
    for ob in (s.query(WireLogPortBinding)
               .filter(WireLogPortBinding.bound_device_id == "dev-sn").all()):
        ob.bound_device_id = None
        ob.allocated_by = None
        ob.allocated_at = None
        ob.allocated_expire = None
    s.commit()
    reg = comres.register(s, tok, {
        "node_id": nid, "local_ip": "10.0.0.88",
        "ports": [{"slot": 1, "com": "COM99", "sn": "SN-HIT-001",
                   "endpoint": "tcp://10.0.0.88:9200", "health_state": "online"}],
    })
check("续约前已分配", reg["ports"][0]["allocated"] is True, reg["ports"][0])

hb = comres.allocated_heartbeat(s, {
    "node_id": nid, "com": "COM99", "endpoint": "tcp://10.0.0.88:9200",
    "allocated_by": "tester-lease", "ttl_s": 60,
})
print("=== ACCEPT-5 renew ===")
print(hb)
check("续约成功", hb["ok"] and hb["allocated"] and hb["allocated_by"] == "tester-lease")
b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
kept_dev = b.bound_device_id
# 人为过期
b.allocated_expire = utcnow() - timedelta(seconds=1)
s.commit()
released = comres.expire_allocations(s)
print("=== ACCEPT-5 expired release ===")
print("released", released)
b = s.query(WireLogPortBinding).filter_by(node_id=nid, slot=1).one()
print(f"  after: allocated_by={b.allocated_by} device={b.bound_device_id} expire={b.allocated_expire}")
check("超时解除占用", b.allocated_by is None and b.allocated_expire is None)
check("超时保留绑定", b.bound_device_id == kept_dev and kept_dev is not None)
check("释放记审计", any(r.get("com") == "COM99" for r in released) or len(released) >= 1)

# ---------- 6) resources 三态 ----------
# allocated：有效占用；bound：有设备无占用；pool：无设备
# 清掉可能占用 SN-HIT-001 的绑定，再造干净样本
for ob in s.query(WireLogPortBinding).all():
    if ob.bound_device_id == "dev-sn" or (ob.allocated_by and "SN" in str(ob.com_name)):
        pass
# 造 allocated
prov_a = wirelog.provision_host(s, display_name="res-a", operator="t")
# 先解除 dev-sn 其它绑定，保证本端口可占用
for ob in (s.query(WireLogPortBinding)
           .filter(WireLogPortBinding.bound_device_id == "dev-sn").all()):
    ob.bound_device_id = None
    ob.allocated_by = None
    ob.allocated_at = None
    ob.allocated_expire = None
s.commit()
comres.register(s, prov_a["node_token"], {
    "node_id": prov_a["node_id"], "local_ip": "10.1.1.1",
    "ports": [{"slot": 1, "com": "COM-A", "sn": "SN-HIT-001",
               "endpoint": "tcp://10.1.1.1:9200", "health_state": "online"}],
})
# 造 bound：有绑定但人为清占用
ba = s.query(WireLogPortBinding).filter_by(node_id=prov_a["node_id"], slot=1).one()
ba.allocated_by = None
ba.allocated_at = None
ba.allocated_expire = None
s.commit()
check("造 bound 样本", ba.bound_device_id is not None and ba.allocated_by is None)

# 再造一条真正 allocated（新 sn 建档）
prov_al = wirelog.provision_host(s, display_name="res-al", operator="t")
comres.register(s, prov_al["node_token"], {
    "node_id": prov_al["node_id"], "local_ip": "10.1.1.3",
    "ports": [{"slot": 1, "com": "COM-AL", "sn": "SN-ALLOC-ONLY",
               "endpoint": "tcp://10.1.1.3:9200", "health_state": "online"}],
})
# 造 pool
prov_p = wirelog.provision_host(s, display_name="res-p", operator="t")
comres.register(s, prov_p["node_token"], {
    "node_id": prov_p["node_id"], "local_ip": "10.1.1.2",
    "ports": [{"slot": 1, "com": "COM-P",
               "endpoint": "tcp://10.1.1.2:9200", "health_state": "online"}],
})
res = comres.list_resources(s)
print("=== ACCEPT-6 resources 三态 ===")
print(f"allocated={len(res['allocated'])} bound={len(res.get('bound', []))} "
      f"pool={len(res['pool'])}")
check("三态键齐全", set(res) >= {"allocated", "bound", "pool"})
check("已分配带 device_id", all(x.get("device_id") for x in res["allocated"]))
check("已绑定带 device_id", all(x.get("device_id") for x in res["bound"]))
check("已绑定未分配", all(x.get("allocated") is False for x in res["bound"]))
check("资源池不带 device_id", all(x.get("device_id") is None for x in res["pool"]))
check("字段含 endpoint/com/allocated",
      all(all(k in x for k in ("endpoint", "node_id", "com", "health_state", "allocated", "state"))
          for x in res["allocated"] + res["bound"] + res["pool"]))
check("三态均非空样本",
      len(res["allocated"]) >= 1 and len(res["bound"]) >= 1 and len(res["pool"]) >= 1)

# ---------- 8) 自动建档 / 幂等 / 冲突 / 无身份不建档 ----------
print("=== ACCEPT-8 auto device register ===")
# 8a 新 sn 建档并绑定
prov_c = wirelog.provision_host(s, display_name="create-n", operator="t")
before_devs = s.query(Device).filter(Device.sn == "SN-NEW-CREATE").count()
r_create = comres.register(s, prov_c["node_token"], {
    "node_id": prov_c["node_id"], "local_ip": "10.2.2.1",
    "ports": [{"slot": 1, "com": "COM-C", "sn": "SN-NEW-CREATE", "mac": "11:22:33:44:55:66",
               "endpoint": "tcp://10.2.2.1:9200", "health_state": "online"}],
})
dev_new = s.query(Device).filter(Device.sn == "SN-NEW-CREATE").one_or_none()
bc = s.query(WireLogPortBinding).filter_by(node_id=prov_c["node_id"], slot=1).one()
aud_c = (s.query(WireLogAudit)
         .filter_by(node_id=prov_c["node_id"], action="comres_device_register")
         .order_by(WireLogAudit.id.desc()).first())
detail_c = json.loads(aud_c.detail) if aud_c and aud_c.detail else {}
print(f"  create device={dev_new.device_id if dev_new else None} "
      f"channel={getattr(dev_new, 'register_channel', None)} "
      f"basis={detail_c.get('basis')} allocated={r_create['ports'][0]['allocated']}")
check("新 sn 建档", before_devs == 0 and dev_new is not None)
check("建档 channel=comres_tcp",
      dev_new is not None and dev_new.register_channel == RegisterChannel.COMRES_TCP)
check("建档绑定", bc.bound_device_id == (dev_new.device_id if dev_new else None))
check("建档 basis=created", detail_c.get("basis") == "created")
check("建档已占用", r_create["ports"][0]["allocated"] is True)

# 8b 同 sn 重复上报幂等
r_idem = comres.register(s, prov_c["node_token"], {
    "node_id": prov_c["node_id"], "local_ip": "10.2.2.1",
    "ports": [{"slot": 1, "com": "COM-C", "sn": "SN-NEW-CREATE",
               "endpoint": "tcp://10.2.2.1:9200", "health_state": "online"}],
})
devs_same = s.query(Device).filter(Device.sn == "SN-NEW-CREATE").count()
bc2 = s.query(WireLogPortBinding).filter_by(node_id=prov_c["node_id"], slot=1).one()
print(f"  idempotent count={devs_same} device={bc2.bound_device_id}")
check("同 sn 幂等不重复建档", devs_same == 1)
check("同 sn 幂等同 device",
      bc2.bound_device_id == (dev_new.device_id if dev_new else None))
check("同 sn 幂等仍占用", r_idem["ports"][0]["allocated"] is True)

# 8c 冲突：另一端口同 sn → 进池，不覆盖先到者
prov_x = wirelog.provision_host(s, display_name="conflict-n", operator="t")
r_conf = comres.register(s, prov_x["node_token"], {
    "node_id": prov_x["node_id"], "local_ip": "10.2.2.2",
    "ports": [{"slot": 1, "com": "COM-X", "sn": "SN-NEW-CREATE",
               "endpoint": "tcp://10.2.2.2:9200", "health_state": "online"}],
})
bx = s.query(WireLogPortBinding).filter_by(node_id=prov_x["node_id"], slot=1).one()
bc_after = s.query(WireLogPortBinding).filter_by(node_id=prov_c["node_id"], slot=1).one()
aud_x = (s.query(WireLogAudit)
         .filter_by(node_id=prov_x["node_id"], action="comres_device_register")
         .order_by(WireLogAudit.id.desc()).first())
detail_x = json.loads(aud_x.detail) if aud_x and aud_x.detail else {}
print(f"  conflict bound={bx.bound_device_id} allocated={r_conf['ports'][0]['allocated']} "
      f"audit={detail_x.get('basis')}/{aud_x.result if aud_x else None} "
      f"keeper={bc_after.bound_device_id}")
check("冲突后者进池", bx.bound_device_id is None and r_conf["ports"][0]["allocated"] is False)
check("冲突不覆盖先到",
      bc_after.bound_device_id == (dev_new.device_id if dev_new else None))
check("冲突记审计告警",
      aud_x is not None and aud_x.result == "conflict"
      and detail_x.get("basis") == "conflict")

# 8d 无 sn/mac 不建档
prov_n = wirelog.provision_host(s, display_name="noid-n", operator="t")
count_before = s.query(Device).count()
r_noid = comres.register(s, prov_n["node_token"], {
    "node_id": prov_n["node_id"], "local_ip": "10.2.2.3",
    "ports": [{"slot": 1, "com": "COM-N",
               "endpoint": "tcp://10.2.2.3:9200", "health_state": "online"}],
})
bn = s.query(WireLogPortBinding).filter_by(node_id=prov_n["node_id"], slot=1).one()
count_after = s.query(Device).count()
print(f"  noid device_delta={count_after - count_before} bound={bn.bound_device_id}")
check("无 sn/mac 不建档", count_after == count_before)
check("无 sn/mac 进池", bn.bound_device_id is None and r_noid["ports"][0]["allocated"] is False)

# ---------- 7) 旧 wirelog 接口域仍可用 ----------
nodes = wirelog.list_nodes(s)
print("=== ACCEPT-7 wirelog nodes ===")
print(f"list_nodes count={len(nodes)} sample_id={nodes[0]['node_id'] if nodes else None}")
check("wirelog.list_nodes 可用", len(nodes) >= 1)

from app.main import app
oa_paths = set(app.openapi().get("paths", {}))
check("路由 /api/comres/register", "/api/comres/register" in oa_paths)
check("路由 /api/wirelog/nodes", "/api/wirelog/nodes" in oa_paths)
print("=== ACCEPT-7 routes ===")
print("comres", sorted(p for p in oa_paths if "/comres" in p))
print("wirelog/nodes", "/api/wirelog/nodes" in oa_paths)

from fastapi.testclient import TestClient
client = TestClient(app)
resp = client.get("/api/wirelog/nodes")
print("GET /api/wirelog/nodes status", resp.status_code)
check("旧 wirelog/nodes 仍响应", resp.status_code in (401, 403, 200))
resp2 = client.post("/api/comres/register", json={})
print("POST /api/comres/register (no auth) status", resp2.status_code)
check("comres/register 无 token→401", resp2.status_code == 401)

# ---------- 9) 全量心跳下线 + 再上报恢复 + auto 保活 / 手工 15min ----------
print("=== ACCEPT-9 heartbeat offline + auto TTL ===")
prov_hb = wirelog.provision_host(s, display_name="hb-offline", operator="t")
tok_hb, nid_hb = prov_hb["node_token"], prov_hb["node_id"]
comres.register(s, tok_hb, {
    "node_id": nid_hb, "local_ip": "10.3.3.1",
    "ports": [
        {"slot": 1, "com": "COM-HB1", "sn": "SN-HB-KEEP",
         "endpoint": "tcp://10.3.3.1:9200", "health_state": "online"},
        {"slot": 2, "com": "COM-HB2", "mac": "22:33:44:55:66:77",
         "endpoint": "tcp://10.3.3.1:9201", "health_state": "online"},
    ],
})
b_hb1 = s.query(WireLogPortBinding).filter_by(node_id=nid_hb, com_name="COM-HB1").one()
b_hb2 = s.query(WireLogPortBinding).filter_by(node_id=nid_hb, com_name="COM-HB2").one()
dev_hb1 = b_hb1.bound_device_id
dev_hb2 = b_hb2.bound_device_id
check("心跳前两口 online", b_hb1.health_state == "online" and b_hb2.health_state == "online")
check("心跳前两口已绑定设备", bool(dev_hb1) and bool(dev_hb2))

# T4 语义：未分配端口未上报 → offline。先解除 HB2 占用（保留绑定），与 T5 已分配跳过解耦。
b_hb2.allocated_by = None
b_hb2.allocated_at = None
b_hb2.allocated_expire = None
s.commit()

# 心跳只报 COM-HB1 → 未分配的 COM-HB2 全量下线
comres.heartbeat(s, tok_hb, {
    "node_id": nid_hb, "local_ip": "10.3.3.1",
    "ports": [{"slot": 1, "com": "COM-HB1", "sn": "SN-HB-KEEP",
               "endpoint": "tcp://10.3.3.1:9200", "health_state": "online"}],
})
b_hb1 = s.query(WireLogPortBinding).filter_by(node_id=nid_hb, com_name="COM-HB1").one()
b_hb2 = s.query(WireLogPortBinding).filter_by(node_id=nid_hb, com_name="COM-HB2").one()
print(f"  after hb: HB1={b_hb1.health_state} HB2={b_hb2.health_state} "
      f"dev2={b_hb2.bound_device_id}")
check("未报端口变 offline", b_hb2.health_state == "offline")
check("未报端口保留设备绑定", b_hb2.bound_device_id == dev_hb2)
aud_off = (s.query(WireLogAudit)
           .filter_by(node_id=nid_hb, action="comres_port_offline")
           .order_by(WireLogAudit.id.desc()).first())
detail_off = json.loads(aud_off.detail) if aud_off and aud_off.detail else {}
check("下线记审计 comres_port_offline",
      aud_off is not None and detail_off.get("com") == "COM-HB2"
      and "was_health_state" in detail_off, detail_off)
res_hb = comres.list_resources(s)
online_hb = (res_hb["allocated"] + res_hb.get("bound", []) + res_hb["pool"])
check("offline 不在在线视图",
      not any(x.get("node_id") == nid_hb and x.get("com") == "COM-HB2" for x in online_hb))
check("仍报端口留在在线视图",
      any(x.get("node_id") == nid_hb and x.get("com") == "COM-HB1" for x in online_hb))

# 再 register 两口 → HB2 恢复 online，设备不丢、不重复建档
devs_before = s.query(Device).filter(Device.mac == "22:33:44:55:66:77").count()
comres.register(s, tok_hb, {
    "node_id": nid_hb, "local_ip": "10.3.3.1",
    "ports": [
        {"slot": 1, "com": "COM-HB1", "sn": "SN-HB-KEEP",
         "endpoint": "tcp://10.3.3.1:9200", "health_state": "online"},
        {"slot": 2, "com": "COM-HB2", "mac": "22:33:44:55:66:77",
         "endpoint": "tcp://10.3.3.1:9201", "health_state": "online"},
    ],
})
b_hb2 = s.query(WireLogPortBinding).filter_by(node_id=nid_hb, com_name="COM-HB2").one()
devs_after = s.query(Device).filter(Device.mac == "22:33:44:55:66:77").count()
print(f"  restore: health={b_hb2.health_state} device={b_hb2.bound_device_id} "
      f"devs={devs_before}->{devs_after}")
check("再上报恢复 online", b_hb2.health_state == "online")
check("再上报设备绑定不丢", b_hb2.bound_device_id == dev_hb2)
check("再上报不重复建档", devs_before == 1 and devs_after == 1)

# auto 绑定：TTL ≥ 7200；3601s 后仍占用，7201s 后释放
prov_auto = wirelog.provision_host(s, display_name="auto-ttl", operator="t")
tok_a, nid_a = prov_auto["node_token"], prov_auto["node_id"]
r_auto = comres.register(s, tok_a, {
    "node_id": nid_a, "local_ip": "10.3.3.8",
    "ports": [{"slot": 1, "com": "COM-AUTO", "sn": "SN-AUTO-TTL",
               "endpoint": "tcp://10.3.3.8:9200", "health_state": "online"}],
})
b_auto = s.query(WireLogPortBinding).filter_by(node_id=nid_a, slot=1).one()
ttl_s = (b_auto.allocated_expire - b_auto.allocated_at).total_seconds()
print(f"  auto ttl_s={ttl_s} by={b_auto.allocated_by} allocated={r_auto['ports'][0]['allocated']}")
check("auto 占用 TTL≥7200", b_auto.allocated_by and b_auto.allocated_by.startswith("auto:")
      and ttl_s >= 7200, f"ttl={ttl_s}")
# 模拟分配后 3601s：expire 仍在未来 → 仍占用
b_auto.allocated_at = utcnow() - timedelta(seconds=3601)
b_auto.allocated_expire = b_auto.allocated_at + timedelta(seconds=int(ttl_s))
s.commit()
comres.expire_allocations(s)
b_auto = s.query(WireLogPortBinding).filter_by(node_id=nid_a, slot=1).one()
check("auto 3601s 后仍已分配",
      b_auto.allocated_by is not None and b_auto.allocated_by.startswith("auto:"),
      f"by={b_auto.allocated_by}")
# 模拟 7201s：expire 已过 → 释放为已绑定未分配
b_auto.allocated_at = utcnow() - timedelta(seconds=7201)
b_auto.allocated_expire = b_auto.allocated_at + timedelta(seconds=7200)
s.commit()
released_auto = comres.expire_allocations(s)
b_auto = s.query(WireLogPortBinding).filter_by(node_id=nid_a, slot=1).one()
print(f"  after 7201s: by={b_auto.allocated_by} device={b_auto.bound_device_id} "
      f"released_n={len(released_auto)}")
check("auto 7201s 后释放占用", b_auto.allocated_by is None and b_auto.allocated_expire is None)
check("auto 7201s 后保留绑定", b_auto.bound_device_id is not None)

# 资源池手工占用仍按 15 分钟释放（非 auto: 前缀；缺省 TTL=ALLOCATED_TTL_S）
prov_m = wirelog.provision_host(s, display_name="manual-ttl", operator="t")
tok_m, nid_m = prov_m["node_token"], prov_m["node_id"]
comres.register(s, tok_m, {
    "node_id": nid_m, "local_ip": "10.3.3.9",
    "ports": [{"slot": 1, "com": "COM-MAN",
               "endpoint": "tcp://10.3.3.9:9200", "health_state": "online"}],
})
b_m = s.query(WireLogPortBinding).filter_by(node_id=nid_m, slot=1).one()
comres._mark_allocated(b_m, by="pool-user")  # 非 auto: → 15min
s.commit()
manual_ttl = (b_m.allocated_expire - b_m.allocated_at).total_seconds()
print(f"  manual ttl_s={manual_ttl} by={b_m.allocated_by}")
check("手工占用缺省 TTL=15min",
      b_m.allocated_by == "pool-user" and abs(manual_ttl - 900) < 2, f"ttl={manual_ttl}")
# 续约路径仍按传入/缺省 15min（与 ACCEPT-5 一致）
hb_m = comres.allocated_heartbeat(s, {
    "node_id": nid_m, "com": "COM-MAN", "endpoint": "tcp://10.3.3.9:9200",
    "allocated_by": "pool-user",
})
b_m = s.query(WireLogPortBinding).filter_by(node_id=nid_m, slot=1).one()
check("手工续约成功", hb_m.get("ok") and b_m.allocated_by == "pool-user")
b_m.allocated_expire = utcnow() - timedelta(seconds=1)
s.commit()
comres.expire_allocations(s)
b_m = s.query(WireLogPortBinding).filter_by(node_id=nid_m, slot=1).one()
check("手工占用 15min 后释放", b_m.allocated_by is None and b_m.allocated_expire is None)

# 节点 stale：超 2 倍心跳无上报 → auto 释放
prov_st = wirelog.provision_host(s, display_name="stale-n", operator="t")
tok_st, nid_st = prov_st["node_token"], prov_st["node_id"]
comres.register(s, tok_st, {
    "node_id": nid_st, "local_ip": "10.3.3.10",
    "ports": [{"slot": 1, "com": "COM-ST", "sn": "SN-STALE-AUTO",
               "endpoint": "tcp://10.3.3.10:9200", "health_state": "online"}],
})
from app.models import WireLogNode
node_st = s.get(WireLogNode, nid_st)
node_st.last_heartbeat_at = utcnow() - timedelta(seconds=7201)
# 把 auto expire 推到未来，确保走 node_stale 路径而非 expire 路径
b_st = s.query(WireLogPortBinding).filter_by(node_id=nid_st, slot=1).one()
kept_st_dev = b_st.bound_device_id
b_st.allocated_expire = utcnow() + timedelta(seconds=3600)
s.commit()
released_st = comres.expire_allocations(s)
b_st = s.query(WireLogPortBinding).filter_by(node_id=nid_st, slot=1).one()
aud_st = (s.query(WireLogAudit)
          .filter_by(node_id=nid_st, action="comres_auto_release_stale")
          .order_by(WireLogAudit.id.desc()).first())
print(f"  stale: by={b_st.allocated_by} device={b_st.bound_device_id} "
      f"audit={aud_st.action if aud_st else None}")
check("节点 stale 释放 auto 占用", b_st.allocated_by is None)
check("节点 stale 保留绑定", b_st.bound_device_id == kept_st_dev)
check("节点 stale 记审计", aud_st is not None)
check("stale 释放命中本端口",
      any(r.get("com") == "COM-ST" for r in released_st), released_st)

# ---------- 10) 已分配端口未上报 → 不置 offline；释放后再未报 → offline ----------
print("=== ACCEPT-10 allocated skip offline ===")
prov_t5 = wirelog.provision_host(s, display_name="alloc-skip", operator="t")
tok_t5, nid_t5 = prov_t5["node_token"], prov_t5["node_id"]
comres.register(s, tok_t5, {
    "node_id": nid_t5, "local_ip": "10.4.4.1",
    "ports": [
        {"slot": 1, "com": "COM-KEEP", "sn": "SN-T5-KEEP",
         "endpoint": "tcp://10.4.4.1:9200", "health_state": "online"},
        {"slot": 2, "com": "COM-HOLD", "mac": "aa:bb:cc:dd:ee:10",
         "endpoint": "tcp://10.4.4.1:9201", "health_state": "online",
         "probe": {"ok": True, "rtt_ms": 1}},
    ],
})
b_hold = s.query(WireLogPortBinding).filter_by(node_id=nid_t5, com_name="COM-HOLD").one()
check("T5 占用前 online+allocated",
      b_hold.health_state == "online" and comres._is_allocated(b_hold),
      f"health={b_hold.health_state} by={b_hold.allocated_by}")
# 心跳不带 COM-HOLD → 应跳过 offline，仍 online，仍在 allocated 视图
comres.heartbeat(s, tok_t5, {
    "node_id": nid_t5, "local_ip": "10.4.4.1",
    "ports": [{"slot": 1, "com": "COM-KEEP", "sn": "SN-T5-KEEP",
               "endpoint": "tcp://10.4.4.1:9200", "health_state": "online"}],
})
b_hold = s.query(WireLogPortBinding).filter_by(node_id=nid_t5, com_name="COM-HOLD").one()
aud_skip = (s.query(WireLogAudit)
            .filter_by(node_id=nid_t5, action="comres_port_skipped_allocated")
            .order_by(WireLogAudit.id.desc()).first())
detail_skip = json.loads(aud_skip.detail) if aud_skip and aud_skip.detail else {}
print(f"  skip: health={b_hold.health_state} by={b_hold.allocated_by} "
      f"audit_com={detail_skip.get('com')}")
check("已分配未报仍 online", b_hold.health_state == "online")
check("已分配未报仍占用", comres._is_allocated(b_hold))
check("已分配跳过记审计",
      aud_skip is not None and detail_skip.get("com") == "COM-HOLD", detail_skip)
res_t5 = comres.list_resources(s)
check("已分配未报仍在 allocated 视图",
      any(x.get("node_id") == nid_t5 and x.get("com") == "COM-HOLD"
          for x in res_t5["allocated"]))

# 释放占用后再心跳不带该端口 → offline，退出在线视图
b_hold.allocated_by = None
b_hold.allocated_at = None
b_hold.allocated_expire = None
s.commit()
comres.heartbeat(s, tok_t5, {
    "node_id": nid_t5, "local_ip": "10.4.4.1",
    "ports": [{"slot": 1, "com": "COM-KEEP", "sn": "SN-T5-KEEP",
               "endpoint": "tcp://10.4.4.1:9200", "health_state": "online"}],
})
b_hold = s.query(WireLogPortBinding).filter_by(node_id=nid_t5, com_name="COM-HOLD").one()
aud_off_t5 = (s.query(WireLogAudit)
              .filter_by(node_id=nid_t5, action="comres_port_offline")
              .order_by(WireLogAudit.id.desc()).first())
detail_off_t5 = json.loads(aud_off_t5.detail) if aud_off_t5 and aud_off_t5.detail else {}
print(f"  after release+hb: health={b_hold.health_state} "
      f"offline_com={detail_off_t5.get('com')}")
check("释放后未报变 offline", b_hold.health_state == "offline")
check("释放后下线记审计",
      aud_off_t5 is not None and detail_off_t5.get("com") == "COM-HOLD", detail_off_t5)
res_t5b = comres.list_resources(s)
online_t5 = res_t5b["allocated"] + res_t5b.get("bound", []) + res_t5b["pool"]
check("释放后 offline 不在在线视图",
      not any(x.get("node_id") == nid_t5 and x.get("com") == "COM-HOLD" for x in online_t5))

# 再次上报恢复 online + probe
comres.heartbeat(s, tok_t5, {
    "node_id": nid_t5, "local_ip": "10.4.4.1",
    "ports": [
        {"slot": 1, "com": "COM-KEEP", "sn": "SN-T5-KEEP",
         "endpoint": "tcp://10.4.4.1:9200", "health_state": "online"},
        {"slot": 2, "com": "COM-HOLD", "mac": "aa:bb:cc:dd:ee:10",
         "endpoint": "tcp://10.4.4.1:9201", "health_state": "online",
         "probe": {"ok": True, "rtt_ms": 2}},
    ],
})
b_hold = s.query(WireLogPortBinding).filter_by(node_id=nid_t5, com_name="COM-HOLD").one()
probe = json.loads(b_hold.probe_json) if b_hold.probe_json else {}
print(f"  restore: health={b_hold.health_state} probe={probe}")
check("再上报恢复 online", b_hold.health_state == "online")
check("再上报刷新 probe", probe.get("rtt_ms") == 2)

print("RESULT", "ALL_PASS" if not fails else f"FAIL={fails}")
raise SystemExit(0 if not fails else 1)
