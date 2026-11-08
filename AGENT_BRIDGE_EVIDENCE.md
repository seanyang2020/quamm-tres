---
task_id: t785
---

# Quamm RES 1.0 收口 验收证据

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `.venv/bin/python -m py_compile app/constants.py app/seed.py app/migrations.py app/main.py app/mcp/server.py app/api/router.py scripts/mcp_client.py scripts/demo_queue_offer.py scripts/verify_v1.py tests/conftest.py tests/test_apply_flow.py tests/test_mcp_http.py` | 0 | 13 个文件 COMPILE_OK |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `30 passed`（原有 26 + 新增 4） |
| 前端构建 | `cd web && npm run build` | 0 | `dist/index.html` + `dist/assets/index-B84NxLay.js` 生成 |

## b. 改动文件清单

```
app/constants.py             APP_VERSION=1.0.0 / APP_NAME / PENDING_CLEANUP_DAYS=30
app/seed.py                  admin@corp.com 管理员 + seed_admin；user01..20 全部 is_admin=False
app/migrations.py            新增：is_admin 列补齐 + 管理员标志修正（幂等，不丢记录）
app/main.py                  lifespan 调 migrate + seed_admin
app/mcp/server.py            res_admin 增 stats；res_health 带 version；res_admin 文档列 action 枚举
app/api/router.py            新增 /api/version、/api/queue、/api/anomalies、/api/audit、/api/settings、
                             POST /api/product-types、POST /api/product-types/{code}/disable；health 带 version
scripts/mcp_client.py        extract_result 解包（空 list 不再 content[0] 越界）+ load_admin_token
scripts/demo_queue_offer.py 新增：排队 offer 真服务实测（confirm 分支 + 超时顺延分支）
scripts/verify_v1.py        新增：一键验收（32 项 PASS/FAIL 表，退出码 0 才算就绪）
tests/conftest.py            预置管理员 token
tests/test_apply_flow.py     whoami 修正（user01 非管理员）+ 新增 admin 断言
tests/test_mcp_http.py       _call_tool 健壮解包 + 空集合 3 条回归
web/src/App.vue              补齐排队/异常/审计/设置四页 + 菜单「异常/待修复」
.gitignore                   忽略 scripts/.demo_admin_token.json
README.md                    Release 1.0 章节 + res_admin action 枚举 + 一键验收/迁移
```

## c. 版本/提交证据

```
$ git log -1 --oneline
a396e36 feat: Quamm RES 1.0 收口——修 3 缺陷、补 Web 八页、排队 offer 实测与一键验收

$ git status --short
（提交后仅 .agent-bridge/ 未跟踪；证据文档本次另起 docs commit）

$ git diff --stat HEAD~1 HEAD
 .gitignore               |   1 +
 README.md                |  62 +++++
 app/api/router.py        | 205 ++++++++++++-
 app/constants.py         |   7 +
 app/main.py              |   5 +-
 app/mcp/server.py        |  55 +++-
 app/seed.py              | 112 +++++--
 app/migrations.py        |  40 +++
 scripts/demo_queue_offer.py | 225 +++++
 scripts/verify_v1.py     | 320 +++++
 scripts/mcp_client.py    |  74 +++-
 tests/conftest.py        |   5 +-
 tests/test_apply_flow.py |  10 +-
 tests/test_mcp_http.py   |  46 ++-
 web/src/App.vue          | 213 +++++++-
 15 files changed
```

- tag：`v1.0.0`（本地，未 push）
- Gerrit change：无/不适用（按要求未 push）

## d. 验证/自测

### 1. 一键验收 `scripts/verify_v1.py`（32 项全 PASS，退出码 0）

```
合计：32 项，PASS 32，FAIL 0
结论：1.0 就绪（全部 PASS）
verify_exit=0
```
覆盖：版本 / whoami（普通 false、管理员 true）/ res_health / list_products /
list_devices / device_info / apply→GRANTED / my_leases / renew / release /
普通 token 调 res_admin 被拒且不留审计记录 / admin stats / ticket 查询 /
排队 offer 流程（confirm）/ 巡检 run 存在 / 8 个 Web 页面接口 200。

### 2. 排队 offer 实测 `scripts/demo_queue_offer.py`（两分支全 PASS，退出码 0）

```
分支 1：holder 占用 → A 排队 pos1 → B pos2 → 释放 → A OFFERED(59s) B 仍 QUEUED
        → A confirm GRANTED → 清场后队列清空
分支 2：同排队 → 释放 → A OFFERED 不确认 → 等 60s 超时 → A CANCELLED(offer_timeout)
        → 顺延 B OFFERED → B confirm GRANTED → 队列清空
结果: PASS（confirm 分支 + 超时顺延分支 全部通过）
```

### 3. 既有脚本保持可用

- `scripts/demo_lease_flow.py` → 退出码 0（查身份→申请→续约→释放→回池）
- `scripts/demo_patrol_cycle.py` → 退出码 0（四阶段闭环，run=4 / notice=10）

### 4. 单测

```
$ .venv/bin/python -m pytest tests/ -q
30 passed in 16.63s
```
新增：`test_whoami_admin_flag`、`test_mcp_http_my_leases_empty_does_not_crash`、
`test_mcp_http_list_devices_empty_does_not_crash`、`test_mcp_http_list_products_never_empty`。

### 5. 老库迁移（不丢记录）

`python -m app.seed` 后：principal 21 条（admin@corp.com is_admin=1，user01..20 is_admin=0），
原有 20 条 user 记录全部保留。
