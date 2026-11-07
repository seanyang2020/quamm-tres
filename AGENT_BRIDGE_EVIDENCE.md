---
task_id: quamm-res-phase1
---

# Quamm RES 第一阶段 验收证据

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| 后端依赖 | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` | 0 | fastapi 0.162 / fastmcp 4.1.6 / sqlalchemy 2.1.8 / apscheduler 3.11 / psycopg 3.3 |
| 导入/建表 | `.venv/bin/python -c "from app.main import app"` | 0 | 10 张表：product_type/device/principal/credential/lease/queue_entry/audit_event/notice/notice_receipt/patrol_run |
| 前端构建 | `cd web && npm install && npm run build` | 0 | 产物 `web/dist/index.html` + `dist/assets/index-*.js`(1.04MB)/`.css`(363KB)，1590 模块 |
| seed | `.venv/bin/python -m app.seed` | 0 | 5 类机型 + 20 token（每人 1 条），打印 MCP 配置 |

## b. 改动文件清单

```
.gitignore  README.md  pytest.ini  requirements.txt  conftest.py
docs/IMPLEMENTATION_REVIEW.md
app/__init__.py  app/main.py  app/config.py  app/db.py  app/models.py
app/constants.py  app/security.py  app/utils.py  app/seed.py
app/api/__init__.py  app/api/router.py
app/domain/__init__.py  app/domain/catalog.py  app/domain/leasing.py
app/domain/queueing.py  app/domain/patrol.py  app/domain/notices.py
app/domain/audit.py  app/domain/probe.py
app/mcp/__init__.py  app/mcp/server.py  app/mcp/middleware.py
app/workers/__init__.py  app/workers/main.py
tests/conftest.py  tests/mock_device.py  tests/test_apply_flow.py
tests/test_registration.py  tests/test_queue.py  tests/test_quota.py
tests/test_credentials.py  tests/test_patrol.py  tests/test_mcp_http.py
deploy/docker-compose.yml  deploy/Dockerfile.app  deploy/Dockerfile.worker  deploy/.env.example
web/package.json  web/package-lock.json  web/vite.config.js
web/index.html  web/src/main.js  web/src/App.vue
```

合计 49 个文件，5412 行新增（`git diff --stat HEAD~1 HEAD`）。

## c. 版本/提交证据

```
$ git log -1 --oneline
9504f99 feat: Quamm RES 第一阶段可运行骨架 + 模拟设备端到端

$ git status --short
（空，工作区干净）

$ git diff --stat HEAD~1 HEAD
 49 files changed, 5412 insertions(+), 8 deletions(-)
```

- Gerrit change：无/不适用（仓库无 remote，未 push）

## d. 验证/自测

| 项 | 命令 | 结果 |
|---|---|---|
| 端到端测试 | `.venv/bin/pytest tests/ -q` | `22 passed`，退出码 0 |
| MCP 鉴权（无 token） | `POST /mcp/` 无 Authorization | 401 |
| MCP initialize + tools/call | 带 token 调 `res_whoami` | 返回 email/配额/is_admin，身份正确 |
| Web API | `GET /api/dashboard` `/api/devices` `/api/leases` `/api/credentials` | 200，返回 JSON |
| Web 看板 | `GET /` | 返回 Vue3 构建产物 index.html |
| 覆盖场景 | 申请→续约→释放；并发抢同一台→排队→释放→队首 offer→超时顺延；配额上限；凭据过期/吊销；探不到设备 pending 不进池；巡检 suspect→offline→恢复+notice；解注册有租约拒绝+标 pending_removal 后自动摘除；MCP streamable HTTP 全链路 | 全部有测试覆盖 |
