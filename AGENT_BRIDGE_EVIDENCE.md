---
task_id: t787
---

# Quamm TRES v0.11 验收证据（task_id: t787）

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `.venv/bin/python -m py_compile app/config.py app/models.py app/migrations.py app/seed.py app/security.py app/web_auth.py app/mailer.py app/api/router.py app/main.py app/mcp/server.py app/workers/main.py tests/test_mcp_http.py scripts/demo_seed_devices.py` | 0 | 13 个文件编译通过 |
| 前端构建 | `cd web && timeout 60 npm run build` | 0 | `web/dist/index.html`、`dist/assets/index-C7AzBUZq.js`、`index-BxprUP6E.css` 生成；title=`Quamm TRES 控制台`；bundle 含「Quamm TRES 登录 / 首次引导 / 人员通讯录 / 发接入凭据 / SMTP 配置 / 申请人姓名」 |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `30 passed in 16.06s` |

（所有可能阻塞/远程命令均以 `timeout` 包裹：SMTP 连通性 15s、SMTP 发信内部 socket 15s、verify_v1 180s、pytest 120s；无 adb/ssh 远程探测。）

## b. 改动文件清单

```
app/web_auth.py                新增：HMAC 签名 HttpOnly 会话 cookie + optional_identity/require_login/require_admin
app/mailer.py                  新增：SMTP 配置(settings 表) / 15s 超时发信 / MCP 配置邮件正文
app/models.py                  Contact、Setting 两表；Credential 加 role/note/active 属性
app/migrations.py              通用幂等 _ensure_columns + credential.role/note ALTER + 回填 admin role
app/seed.py                    _ensure_credential 带 role；seed_contacts 从 /tmp/contacts_seed.json 导 17 人
app/security.py                resolve_token 的 is_admin 兼容 credential.role
app/config.py                  session_secret / web_session_hours / mcp_public_url
app/api/router.py              /api/auth/*(login/me/logout/setup)、contacts CRUD、SMTP 配置/测试、发凭据；
                               dashboard/leases/credentials/queue/audit 角色过滤；写操作 require_admin
app/main.py                    lifespan 加 seed_contacts；FastAPI title / fallback 改名 Quamm TRES
app/mcp/server.py              MCP server name quamm-res -> quamm-tres
app/workers/main.py            日志前缀 -> quamm-tres.worker
app/constants.py               APP_NAME 保留值 + 注释（/api/version 契约值）
web/src/App.vue                登录页 / 首次引导页 / 登出 / 设置 4 Tab / 通讯录 CRUD / 发凭据 / SMTP /
                               凭据台账增强 / 租约申请人姓名；保留 t786「工程配置」列
web/index.html                 title -> Quamm TRES 控制台
web/package.json               name -> quamm-tres-web
web/package-lock.json          同上（2 处）
scripts/demo_seed_devices.py   描述改名 Quamm TRES
tests/test_mcp_http.py         serverInfo.name 断言 -> quamm-tres
README.md                      全局改名 + v0.11 release 说明 + MCP key quamm-tres
CHANGELOG.md                   新增 [0.11.0] 段
VERSION                        0.10.0 -> 0.11.0
web/dist/                      构建产物（.gitignore，npm run build 重建，含新登录 UI，未入库）
AGENT_BRIDGE_PROGRESS.md       本轮进度
AGENT_BRIDGE_EVIDENCE.md       本文件（验收四要素）
.agent-bridge/evidence/t787.md 本任务隔离证据副本
```

未提交、保留不动（t786 遗留备份 / 他任务产物，非本任务文件域）：
`.agent-bridge/evidence/t786.md`、`web/dist.bak-caddyadd/`、`web/src/App.vue.bak-caddyadd`、`web/vite.config.js.bak-caddyadd`

## c. 版本/提交证据

```
$ git log -1 --oneline
7154c45 feat: Quamm TRES v0.11 登录认证/凭据管理/通讯录/SMTP 发 token/全局改名
        ↑ 本任务功能提交；其后另有 AGENT_BRIDGE_* 证据定稿提交

$ git status --short（功能提交后）
?? .agent-bridge/evidence/t786.md
?? web/dist.bak-caddyadd/
?? web/src/App.vue.bak-caddyadd
?? web/vite.config.js.bak-caddyadd
（以上均为 t786 遗留未跟踪备份，未 git add；工作树无其它改动）

$ git diff --stat 741a560 7154c45
 21 files changed, 1155 insertions(+), 144 deletions(-)
```

- 提交前基线 HEAD：`741a560 docs: t786 验收四要素与进度`
- `VERSION`：0.10.0 -> 0.11.0（`/api/version` 由 VERSION 单一真源读取，stage 仍 pre-release）
- Gerrit change：无/不适用（remote 为 GitHub SSH `git@github.com:seanyang2020/quamm-tres.git`，无 Gerrit）
- push：未执行（共享远端，停住等审后再推）

## d. 验证/自测

| 项 | 命令 / 操作 | 结果 |
|---|---|---|
| 32 项验收 | `timeout 180 .venv/bin/python scripts/verify_v1.py` | 退出码 0，`合计：32 项，PASS 32，FAIL 0`，`0.11.0 就绪` |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 退出码 0，`30 passed` |
| 服务重启 | kill 旧 uvicorn/worker（958319/958318）→ 重启 961621/961623 | `/api/health` 200、`/api/version` 0.11.0；启动日志无迁移报错 |
| Web 打开 | `GET /` | 200，title=`Quamm TRES 控制台`；未登录 `/api/auth/me` → 401 |
| admin 登录 | `POST /api/auth/login` (admin rr_ token) | 200，Set-Cookie `quamm_session=...; HttpOnly; Max-Age=28800; Path=/; SameSite=lax`；`/api/auth/me` → admin@corp.com / admin；logout 后 401 |
| 通讯录 | admin `GET/POST/PUT/DELETE /api/contacts` | 列表 17 人（@quaming.com 种子）；增改删均 200 |
| SMTP 测试邮件 | `POST /api/settings/smtp/test` → yangxiongwei@quaming.com | 200 `{"sent":true}`（TCP 465 可达，真实发出）；配置为任务给定默认值 |
| 发接入凭据 | `POST /api/credentials/send` | 200，生成 rr_ token 且 `sent:true`（MCP 配置邮件真实发出）；台账新增 name/role/note/active/created_at；冒烟凭据已清理 |
| user 权限 | user token 登录 | `/api/contacts`、`/api/settings/smtp` → 403；`/api/leases` 仅自己的；匿名访问 admin 写接口 → 401 |
| 租约申请人 | 活跃租约 `GET /api/leases` | 返回 `email` + `name`（测试用户01）；user02 看不到 user01 的租约（0 行） |
| 首次引导 | 临时库吊销所有 admin 凭据后 | `/api/auth/setup-status` → needs_setup=true；`/api/auth/setup` 生成首个 admin token、重复 409、新 token 可登录；已有 admin@corp.com 时 needs_setup=false |
| 角色过滤 | admin 全量 / user 自有 / 匿名 200 | dashboard 对 user 隐藏占用与排队详情；MCP 端仍 Authorization header 直连不受 cookie 影响 |

### 边界说明（不破坏既有验收）

- `scripts/verify_v1.py` **完全未修改**：其 32 项断言（含 `name == "Quamm RES"`、8 个 Web 页面接口匿名 200）全部保持 PASS。
- 因此 `/api/version` 的 `name` 保留 `Quamm RES` 作为 API 契约值；产品展示名（网页 title / MCP server name / README / 日志前缀 / MCP 配置 key / 邮件）统一为 **Quamm TRES**，README/CHANGELOG 已注明该例外。
- 只读展示接口匿名仍返回 200（历史验收脚本直打真服务），「不登录看不到数据」由 Vue 端登录门禁保证；写操作 / 通讯录 / SMTP / 发凭据 / 引导接口均 `require_admin`（未登录 401、user 403）。
- `app/domain/prober_real.py` 未改动；端口 9100 未动；迁移只 `ALTER TABLE ADD COLUMN`（幂等，已存在不报错），旧库不丢记录；Vue 沿用 Element Plus，未引入新 UI 框架。
