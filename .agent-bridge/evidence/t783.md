---
task_id: t783
---

# Quamm RES：用 token 注册模拟设备 + 端到端演示 验收证据

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| 脚本语法 | `.venv/bin/python -m py_compile scripts/mcp_client.py scripts/demo_seed_devices.py scripts/demo_lease_flow.py` | 0 | 3 个脚本编译通过 |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `23 passed in 12.41s` |
| 服务加载 | `.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080`（环境自动拉起）后 `GET /api/health` | 0 | `{"status":"ok","last_patrol_at":null}`，新进程加载了探测后端初始化修复 |

## b. 改动文件清单

```
.gitignore
README.md
app/config.py
app/domain/catalog.py
app/main.py
app/seed.py
tests/test_registration.py
scripts/mcp_client.py            （新增）
scripts/demo_seed_devices.py     （新增）
scripts/demo_lease_flow.py       （新增）
```

## c. 版本/提交证据

```
$ git log -1 --oneline
c748fd9 feat: 用 token 注册 20 台模拟设备 + 租约端到端演示

$ git diff --stat HEAD~1 HEAD
 .gitignore                   |   1 +
 README.md                    |  28 ++++++
 app/config.py                |   4 +-
 app/domain/catalog.py        |   9 +-
 app/main.py                  |   4 +
 app/seed.py                  |  25 +++++-
 scripts/demo_lease_flow.py   | 163 +++++++++++++++++++++++++++++++++
 scripts/demo_seed_devices.py | 209 +++++++++++++++++++++++++++++++++++++++++++
 scripts/mcp_client.py        | 138 ++++++++++++++++++++++++++++++++++++
 tests/test_registration.py   |  19 ++++
 10 files changed, 593 insertions(+), 7 deletions(-)
```

- Gerrit change：无/不适用（按要求未 push）

## d. 验证/自测

| 项 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `.venv/bin/python -m pytest tests/ -q` | 23 passed，退出码 0（新增重复注册幂等用例） |
| 设备注册（首次） | `.venv/bin/python scripts/demo_seed_devices.py` | 退出码 0；新建 20 / 命中 0 / 失败 0；`total_devices=20`，`by_status={'available': 20}` |
| 设备注册（第二次，幂等） | 同一条命令 | 退出码 0；新建 0 / 命中 20 / 失败 0；`total_devices=20`，无位置重复 |
| 租约演示（两次） | `.venv/bin/python scripts/demo_lease_flow.py` | 两次均退出码 0；身份/余量/候选/申请(adb 地址+到期)/持有/续约(到期后移)/释放/回池全部通过 |
| /api/devices 复核 | `curl /api/devices?limit=500` | 20 条，position_code 唯一 20 个，registered_by 覆盖 user01~user20 |
| 端口清理 | `ss -ltn \| grep 70(01-20)` | 无输出（mock 端点全部关闭，无残留） |

关键响应（第一次演示）：

```
res_apply   -> state=GRANTED adb_address=127.0.0.1:7006 expires_at=2026-11-08T08:59:06
res_renew   -> 2026-11-08T08:59:06 -> 2026-11-08T09:29:06
res_release -> state=RELEASED ok=True
回池        -> QM102V available=4，原设备在池中=True
```
