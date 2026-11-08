---
task_id: t785
---

# Quamm RES 0.10.0 收口 验收证据

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `.venv/bin/python -m py_compile app/constants.py app/seed.py app/migrations.py app/main.py app/mcp/server.py app/api/router.py scripts/mcp_client.py scripts/demo_queue_offer.py scripts/verify_v1.py tests/conftest.py tests/test_apply_flow.py tests/test_mcp_http.py` | 0 | 13 个文件 COMPILE_OK |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `30 passed`（原有 26 + 新增 4） |
| 前端构建 | `cd web && npm run build` | 0 | `dist/index.html` + `dist/assets/index-B84NxLay.js` 生成 |
| 版本读取 | `.venv/bin/python -c "from app.constants import version_info; print(version_info())"` | 0 | `{'version': '0.10.0', 'name': 'Quamm RES', 'stage': 'pre-release'}` |

## b. 改动文件清单

```
VERSION                       新增：0.10.0（版本号唯一真源）
CHANGELOG.md                  新增：0.10.0 变更记录
app/constants.py              get_version()/version_info() 读 VERSION 文件，移除硬编码 APP_VERSION
app/seed.py                   admin@corp.com 管理员 + seed_admin；user01..20 全部 is_admin=False
app/migrations.py             新增：is_admin 列补齐 + 管理员标志修正（幂等，不丢记录）
app/main.py                   lifespan 调 migrate + seed_admin
app/mcp/server.py             res_admin 增 stats；res_health 带 version（读 VERSION）
app/api/router.py             新增 /api/version、/api/queue、/api/anomalies、/api/audit、/api/settings、
                             POST /api/product-types、POST /api/product-types/{code}/disable；health 带 version
scripts/mcp_client.py         extract_result 解包（空 list 不再 content[0] 越界）+ load_admin_token
scripts/demo_queue_offer.py  新增：排队 offer 真服务实测（confirm 分支 + 超时顺延分支）
scripts/verify_v1.py         新增：一键验收（32 项 PASS/FAIL 表，期望版本读 VERSION 文件）
tests/conftest.py             预置管理员 token
tests/test_apply_flow.py      whoami 修正（user01 非管理员）+ 新增 admin 断言
tests/test_mcp_http.py        _call_tool 健壮解包 + 空集合 3 条回归
web/src/App.vue               补齐排队/异常/审计/设置四页 + 菜单「异常/待修复」
deploy/Dockerfile.app         COPY VERSION
deploy/Dockerfile.worker      COPY VERSION
.gitignore                    忽略 scripts/.demo_admin_token.json
README.md                     版本管理一节 + Release 0.10.0 + res_admin action 枚举
```

## c. 版本/提交证据

```
$ git log -1 --oneline
（见下方最新提交；功能提交 + 证据提交）

$ git status --short
（提交后仅 .agent-bridge/ 未跟踪）

$ git diff --stat HEAD~N
 15+ 文件改动（app/ scripts/ tests/ web/ deploy/ README/ CHANGELOG/ VERSION）
```

- tag：`v0.10.0`（本地，未 push）
- Gerrit change：无/不适用（按要求未 push）

## d. 验证/自测

### 1. 一键验收 `scripts/verify_v1.py`（32 项全 PASS，退出码 0）

```
版本号   {'version': '0.10.0', 'name': 'Quamm RES', 'stage': 'pre-release'}  PASS
res_health  status=ok version=0.10.0                                        PASS
...
合计：32 项，PASS 32，FAIL 0
结论：0.10.0 就绪（全部 PASS）
```

### 2. 排队 offer 实测 `scripts/demo_queue_offer.py`（两分支全 PASS，退出码 0）

confirm 分支（A 窗口内 res_ticket 确认 → GRANTED）+ 超时顺延分支（A 不确认 → 60s 超时 → 顺延 B）。

### 3. 既有脚本保持可用

- `scripts/demo_lease_flow.py` → 退出码 0
- `scripts/demo_patrol_cycle.py` → 退出码 0

### 4. 单测

```
$ .venv/bin/python -m pytest tests/ -q
30 passed in 16.86s
```

### 5. 老库迁移（不丢记录）

principal 21 条（admin@corp.com is_admin=1，user01..20 is_admin=0），原有 20 条记录保留。
