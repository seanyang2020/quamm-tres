---
task_id: t805
---

# Quamm TRES v0.24.0 验收证据（task_id: t805）

> 设备截图 Web 预览增强 + 图片生命周期管理；确认截图与 DB 升级能力完整可用。
> 未 `git commit` / 未 push（由用户提交推送），停住等审。
> 不提交两个 `*.bak-caddyadd`；`.agent-bridge/evidence/t804.md` 为上一任务既存未跟踪文件，未触碰。

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译（14 文件） | `timeout 90 .venv/bin/python -m py_compile app/models.py app/migrations.py app/config.py app/runtime_config.py app/screenshot.py app/screenshot_lifecycle.py app/api/router.py app/mcp/server.py app/domain/leasing.py app/workers/main.py app/db_source.py scripts/verify_screenshot.py scripts/verify_screenshot_lifecycle.py tests/test_screenshot.py tests/test_screenshot_lifecycle.py` | 0 | 14 个文件全部编译通过，无语法错误 |
| 全模块导入 + 版本 | `timeout 60 .venv/bin/python -c "import app.main, app.api.router, app.mcp.server, app.screenshot, app.screenshot_lifecycle, app.domain.leasing, app.workers.main, app.db_source, app.runtime_config; from app.constants import version_info; print(version_info())"` | 0 | 无循环导入/异常；`version_info()={'version':'0.24.0', ...}`（VERSION 已 bump） |
| 前端构建 | `cd web && timeout 240 npm run build` | 0 | `web/dist/index.html`、`web/dist/assets/index-Dx4eRcAc.js`(1,111.87 kB)、`index-CMneF6Yw.css`(364.73 kB) 重建成功；chunk 体积告警为既存、非错误 |
| 构建产物校验 | `grep -o "重新截图\|截图记录\|截图存储\|screenshot-records\|立即清理" web/dist/assets/*.js` | 0 | 命中：重新截图×4、截图记录×3、截图存储×2、screenshot-records×4、立即清理×1，新增 UI 已入 bundle |
| 截图单测 | `timeout 120 .venv/bin/python -m pytest tests/test_screenshot.py tests/test_screenshot_lifecycle.py -q` | 0 | `40 passed`（原 27 + 新增 13） |
| 全量回归 | `timeout 240 .venv/bin/python -m pytest tests/ -q` | 1 | `17 failed, 70 passed`；17 项为仓库既存失败，见下方基线对照，无新增回归 |
| 干净 HEAD 基线 | `git worktree add --detach /tmp/quamm-head-clean-t805 HEAD` 后同命令 | 1 | `17 failed, 57 passed`；失败名单与工作树 17 项完全一致；工作树多出的 13 passed 即本任务新增单测（已 `git worktree remove` 清理） |
| 迁移幂等 | `timeout 60 .venv/bin/python -c "... DROP TABLE screenshot_record; migrate(s) x2 ..."`（临时库） | 0 | 删表后 migrate #1 补建、migrate #2 不报错；`screenshot_record` 10 列齐全 |

## b. 改动文件清单

修改（13 个已跟踪文件）：
- `VERSION`（0.23.0 → 0.24.0）
- `CHANGELOG.md`（增 `[0.24.0] - 2026-11-09`）
- `app/models.py`（新增 `ScreenshotRecord` / 表 `screenshot_record`）
- `app/migrations.py`（`_ensure_screenshot_table` inspect 判表幂等兜底 + docstring）
- `app/config.py`（4 个截图策略默认值）
- `app/runtime_config.py`（白名单加 4 键；修复 `set_runtime_value` bool 存值）
- `app/screenshot.py`（`created_by` 参数、`_write_record`、`list_screenshots`/`delete_screenshot`、成功后写记录+自动 per-device prune）
- `app/screenshot_lifecycle.py`（新建：serialize/stats/delete_device_screenshots/prune_device/prune_global）
- `app/api/router.py`（4 个管理员截图记录接口 + POST 截图传 created_by）
- `app/mcp/server.py`（`res_screenshot` 传 created_by）
- `app/domain/leasing.py`（`_reclaim()` 物理闸门附近 best-effort 释放清理）
- `app/workers/main.py`（`_screenshot_prune_job` 每小时全局 prune）
- `scripts/verify_screenshot.py`（独立进程先 `init_db()`，因截图现在写库）
- `web/src/App.vue`（预览增强 / 下载 / 重新截图 / 旋转 / apiUrl / 截图记录入口 / 截图存储设置块）

新增（3 个）：
- `app/screenshot_lifecycle.py`
- `scripts/verify_screenshot_lifecycle.py`
- `tests/test_screenshot_lifecycle.py`（13 项单测）

不纳入提交（显式列出）：
- `web/src/App.vue.bak-caddyadd`、`web/vite.config.js.bak-caddyadd`（既存未跟踪，明确不提交）
- `web/dist.bak-caddyadd/`、`.agent-bridge/evidence/t804.md`（既存，未触碰）
- `web/dist/`、`app/var/screenshots/*.jpg`（运行产物，gitignore）

建议显式提交清单（供提交时逐项列出，勿用 `git add -A`）：
`VERSION CHANGELOG.md app/models.py app/migrations.py app/config.py app/runtime_config.py app/screenshot.py app/screenshot_lifecycle.py app/api/router.py app/mcp/server.py app/domain/leasing.py app/workers/main.py scripts/verify_screenshot.py scripts/verify_screenshot_lifecycle.py tests/test_screenshot_lifecycle.py web/src/App.vue AGENT_BRIDGE_PROGRESS.md AGENT_BRIDGE_EVIDENCE.md`

## c. 版本/提交证据

```
$ git log -1 --oneline
35f4ec9 v0.23.0: 设备截图+芯片/浮点ABI判定，MCP res_screenshot，品类芯片字段

$ git status --short
 M CHANGELOG.md
 M VERSION
 M app/api/router.py
 M app/config.py
 M app/domain/leasing.py
 M app/mcp/server.py
 M app/migrations.py
 M app/models.py
 M app/runtime_config.py
 M app/screenshot.py
 M app/workers/main.py
 M scripts/verify_screenshot.py
 M web/src/App.vue
?? .agent-bridge/evidence/t804.md     # 上一任务既存，不提交
?? app/screenshot_lifecycle.py        # 本任务新增
?? scripts/verify_screenshot_lifecycle.py  # 本任务新增
?? tests/test_screenshot_lifecycle.py # 本任务新增
?? web/src/App.vue.bak-caddyadd       # 不提交
?? web/vite.config.js.bak-caddyadd    # 不提交

$ git diff --stat
 AGENT_BRIDGE_EVIDENCE.md     | 173 ++++++++++++++++----------------   # 本证据文件（自身，随收尾更新）
 AGENT_BRIDGE_PROGRESS.md     |  43 ++++----                         # 本任务进度文件
 CHANGELOG.md                 |  57 +++++++++++
 VERSION                      |   2 +-
 app/api/router.py            |  54 +++++++++-
 app/config.py                |   6 ++
 app/domain/leasing.py        |  16 +++
 app/mcp/server.py            |   1 +
 app/migrations.py            |  16 +++
 app/models.py                |  21 ++++
 app/runtime_config.py        |  14 ++-
 app/screenshot.py            | 103 ++++++++++++++++++-
 app/workers/main.py          |  22 +++-
 scripts/verify_screenshot.py |   4 +
 web/src/App.vue              | 233 +++++++++++++++++++++++++++++++++++++++----
 15 files changed, 629 insertions(+), 136 deletions
```

- `VERSION` = 0.24.0；`CHANGELOG.md` 已加 `[0.24.0] - 2026-11-09`
- Gerrit change：无/不适用
- 本任务本地改动尚未 `git commit`；未 push（按要求由用户提交推送）
- 未重启 uvicorn/worker：新 REST/MCP 端点需进程重启后生效；验证均通过「直接调用处理函数 + 全新进程导入 + 临时库」完成

## d. 验证/自测

| 功能 | 命令/方式 | 结果 |
|---|---|---|
| 实机截图 + 记录入库 + per-device prune（要求项） | `timeout 60 .venv/bin/python scripts/verify_screenshot_lifecycle.py` | exit 0 / PASS：对 172.16.10.246:5555 连拍 3 张（临时 SQLite 库不污染线上）；记录数 1→2→2（`screenshot_max_per_device=2`），第 3 张触发 prune 删最旧 1 张，磁盘仅留最新 2 张；`list_screenshots`=2、`stats={"count":2,"total_bytes":14428,"devices":1}`、单删/全局 prune 通过 |
| 截图 URL 公网隧道/子路径解析 | `timeout 20 node -e "... new URL(rel, href) ..."` | `./`+`/quamm/`+`/quamm/index.html`+`/`+`VITE_BASE=/quamm/` 5 种 BASE 均解析到正确的 `/api/screenshots/a.jpg` 绝对地址 |
| prune / 清理纯函数单测 | `timeout 120 .venv/bin/python -m pytest tests/test_screenshot_lifecycle.py -q` | exit 0 / `13 passed`：per-device 保留最新 N、全局 retention_days/total_max、释放清理（含配置开关开/关）、文件缺失不报错、统计、序列化、leasing 整合、rotate 透传+mocked 自动 prune |
| 释放清理整合 | `test_release_cleans_screenshots_and_still_succeeds` / `test_release_keeps_screenshots_when_disabled` | release 成功且截图清空；开关关闭时截图保留；清理挂外层事务（commit=False），失败不影响释放 |
| REST 管理接口直调 | `timeout 60 .venv/bin/python -c "... router.list_screenshot_records/screenshot_stats/prune_screenshot_records/remove_screenshot_record/get_screenshot ..."`（临时库） | 列举 3 条、stats、全局 prune、单删后剩 2 条、`get_screenshot('../evil.jpg')` → HTTP 400（防穿越未回归） |
| REST HTTP 路由（TestClient + 依赖覆写） | `timeout 60 .venv/bin/python -c "... TestClient(app) ..."`（临时库） | `GET /api/screenshot-records` 200、`GET /api/screenshot-records/stats` 200（未被 `/{file_name}` 影子匹配）、`POST /api/screenshot-records/prune` 200、`DELETE /api/screenshot-records/shot_x` 200 |
| 在线配置（4 键 + bool 开关） | `TestClient` `GET/PUT /api/settings` | `GET` 返回 4 键默认 10/14/500/true；`PUT {"screenshot_cleanup_on_release": false}` → 200 返回 False，再 `GET` 仍为 False（bool 存值修复生效，开关可关） |
| 删除审计覆盖 | `test_delete_screenshot_by_file_name` / 直调 `delete_screenshot` | 单张删除（manual_delete）、per-device prune（per_device_limit）、全局 prune（retention_days/total_max）、释放清理（release）均落 `AuditEvent(action=screenshot_prune, detail={count, reason})` |
| DB 升级能力只读核对（要求项 5） | `test -x /home/scm/bin/xosadb2update` + `timeout 30 .venv/bin/python -c "... db_source ..."` | exit 0：`XOSADB2UPDATE_PATH=/home/scm/bin/xosadb2update` 存在且可执行；`upgrade()` 默认 `update_args='-f -a -e data'`（保留 data 区）未变；`probe_directory/find_versions/normalize_source/list_packages/device_db_version/url_ok` 均可调用；`normalize_source('daily', proj)` 返回正确 URL；未改默认、未对真实设备执行升级 |
| 迁移幂等 | 临时库 `DROP TABLE screenshot_record` → `migrate()` ×2 | 补建成功、二次不报错、10 列齐全 |
| 前端 | `cd web && npm run build` | exit 0，dist 重建、新增 UI 标记命中 |

### 备注
- 所有可能阻塞/远程命令均用 `timeout` 包裹（adb connect / adb push / adb shell / 实机脚本 60s、python 检查 30-60s、npm build 240s、pytest 240s）；`app/screenshot.py` 内部每条 adb 还带 GNU `timeout 15` + subprocess 双保险。
- 设备端 vendor 二进制 `xosscreen -r 90/180/270` 实测 Segmentation fault（`-r 90` 直接跑 RC=139，属二进制自身 bug，非本系统逻辑）；实机验证统一 `rotate=0`，前端仍按所选 rotate 调用 API，rotate 接收/透传由 mocked 单测覆盖。
- 未提交两个 `*.bak-caddyadd`；未写入线上 mock 业务数据（验证用临时 SQLite 库）；未 push；停住等审。
