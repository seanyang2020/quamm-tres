---
task_id: t788
---

# Quamm TRES v0.12 验收证据（task_id: t788）

> 设备类型元数据扩展 + 排队按机型申请。改动均为本地工作树（未提交、未 push，停住等审）。

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `.venv/bin/python -m py_compile app/models.py app/migrations.py app/domain/catalog.py app/domain/leasing.py app/mcp/server.py app/api/router.py app/main.py app/workers/main.py scripts/verify_v1.py tests/test_queue.py tests/test_apply_flow.py` | 0 | 11 个文件 COMPILE_OK |
| 前端构建 | `cd web && timeout 120 npm run build` | 0 | `web/dist/index.html`、`web/dist/assets/index-CcfyzpOs.js`(1,064.35 kB)、`index-BxprUP6E.css` 生成；bundle grep 命中「设备类型 / agent_label / 完整度 / defconfig / 信息不全 / 已等待」 |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `33 passed in 17.98s`（原 30 + 新增 3） |
| 幂等迁移 | `timeout 60 .venv/bin/python -c "... migrate(s) ..."`（对实时库） | 0 | product_type 列确认含 `defconfig/sdk_repo_cmd/build_cmd/image_path/jenkins_url/notes` 共 15 列 |

（所有可能阻塞/远程命令均以 `timeout` 包裹：py_compile/npm build 120s、pytest 200s、verify 240s、migrate 60s、各 curl 15s；全程未使用 adb/ssh/scp/串口探测。`setsid nohup ... &` 仅用于启动常驻守护进程，非阻塞命令。）

## b. 改动文件清单

```
app/models.py                 ProductType 加 6 列（defconfig/sdk_repo_cmd/build_cmd/image_path/jenkins_url/notes）
app/migrations.py             _PRODUCT_TYPE_COLUMNS 幂等 ALTER（migrate() 调 _ensure_columns）
app/domain/catalog.py         COMPLETE_FIELDS/METADATA_FIELDS + product_complete()；
                              list_products 输出补 6 字段 + complete/complete_label + leased/queued（保留 total/available/occupied）
app/domain/leasing.py         _resolve_target()（product_type / device_id 二选一 + target 旧用法）；
                              apply() 改签名，QUEUED 返回带 target_type/target_code
app/mcp/server.py             res_apply 增 product_type/device_id 两参数（保留 target 兼容）
app/api/router.py             /api/queue 补 name/agent_label/model/waited_seconds/waited_text；
                              新增 GET /api/product-types、PUT /api/product-types/{type_code}（require_admin）
scripts/verify_v1.py          _run_queue_offer 改按机型（product_type），新增 _prepare_product_pool（REST 建机型 + MCP 注册 1 台）；
                              保持 32 项检查数不变
tests/test_queue.py           新增 2 个按机型用例（机型队列唤醒 / device_id 与二选一错误）
tests/test_apply_flow.py      新增 1 个用例（6 字段/完整度/leased/queued 统计）
web/src/App.vue               设置页新增「设备类型」Tab（列表+完整度标签+编辑弹窗，admin 可改）；
                              排队「全部条目」表加可展开行（agent_label + email/name + 机型 + 已等待）；看板机型余量加 leased/queued 列
VERSION                        0.11.0 -> 0.12.0
CHANGELOG.md                   新增 [0.12.0] 段
TODO.md                        新建，追加 5 条规划项（不实现）
AGENT_BRIDGE_PROGRESS.md       本轮进度（task_id: t788）
AGENT_BRIDGE_EVIDENCE.md       本文件（验收四要素，task_id: t788）
web/dist/                      构建产物（.gitignore，npm run build 重建，未入库）
```

未提交、保留不动（t786 遗留 / 他任务产物，非本任务文件域）：
`.agent-bridge/evidence/t786.md`、`web/dist.bak-caddyadd/`、`web/src/App.vue.bak-caddyadd`、`web/vite.config.js.bak-caddyadd`

## c. 版本/提交证据

```
$ git log -1 --oneline
c7d3b69 docs: t787 验收四要素与进度（v0.11 登录/凭据/通讯录/SMTP 证据）
        ↑ 本任务基线 HEAD（未新提交，工作树改动停住等审）

$ git status --short
 M AGENT_BRIDGE_EVIDENCE.md
 M AGENT_BRIDGE_PROGRESS.md
 M CHANGELOG.md
 M VERSION
 M app/api/router.py
 M app/domain/catalog.py
 M app/domain/leasing.py
 M app/mcp/server.py
 M app/migrations.py
 M app/models.py
 M scripts/verify_v1.py
 M tests/test_apply_flow.py
 M tests/test_queue.py
 M web/src/App.vue
?? TODO.md
?? .agent-bridge/evidence/t786.md            （t786 遗留）
?? web/dist.bak-caddyadd/                    （t786 遗留）
?? web/src/App.vue.bak-caddyadd              （t786 遗留）
?? web/vite.config.js.bak-caddyadd           （t786 遗留）

$ git diff --stat
 14 files changed, 538 insertions(+), 135 deletions(-)
   （另新增未跟踪 TODO.md）
```

- 基线 HEAD：`c7d3b69`；本任务未 commit、未 push（停住等审）。
- `VERSION`：0.11.0 -> 0.12.0；`/api/version` 由 VERSION 单一真源读取，`name` 保留 `Quamm RES` 契约值、`stage=pre-release`。
- Gerrit change：无/不适用（remote 为 GitHub SSH `git@github.com:seanyang2020/quamm-tres.git`，无 Gerrit）。

## d. 验证/自测

| 项 | 命令 / 操作 | 结果 |
|---|---|---|
| 32 项验收（强制） | `timeout 240 .venv/bin/python scripts/verify_v1.py` | 退出码 0，`合计：32 项，PASS 32，FAIL 0`，`0.12.0 就绪（全部 PASS）`；其中 #16-23 为按机型排队（holder 占机型设备 → A/B 进机型队列 → 释放 → 机型队首 A 收 offer → 确认 → 清场） |
| 单测 | `timeout 200 .venv/bin/python -m pytest tests/ -q` | 退出码 0，`33 passed in 17.98s` |
| 服务重启 | kill 旧 963951/961623 → 重启 uvicorn(966592)+worker(966593)，`setsid nohup` 脱离会话 | `/api/health` 200 `{"status":"ok","version":"0.12.0"}`；`/api/version` 0.12.0；启动日志无迁移报错；两进程 SID=PID 常驻，`ss :9100` LISTEN |
| Web 产物 | `GET /` / `GET /assets/index-CcfyzpOs.js` | 200；返回 `Quamm TRES 控制台` 且引用新 bundle |
| 设备类型 Tab/REST | `POST /api/product-types`（admin）→ `GET /api/product-types` → `PUT`（填 4 列）→ `PUT`（清 image_path） | 200；字段齐全；初始 `信息不全` → 填齐 `完整` → 缺一列回 `信息不全`；匿名 PUT → 401 |
| 排队展开字段 | `GET /api/queue` | entries 含 `name/agent_label/model/waited_text/waited_seconds` |
| res_list_products | MCP `res_list_products` | 每机型含 `total/available/leased/queued` + `complete_label` |
| res_apply 二选一 | MCP `res_apply` | `device_id=` → GRANTED；同时给 `product_type+device_id` → `AMBIGUOUS_TARGET`；未知机型 → `UNKNOWN_TARGET`；旧 `target=` 仍可用 |
| 幂等迁移 | 实时库连续 migrate | 6 列存在，重复执行不报错、不丢记录 |

### 边界说明（不破坏既有验收）

- `scripts/verify_v1.py` 32 项检查数不变、全部 PASS；排队用例已按要求同步改为按机型（`product_type`），其余 `target=device_id` 用法保留兼容。
- `res_apply` 保留旧单参数 `target`（device_id 或 type_code），历史客户端 / httpx 验收 / pytest 直调均不受影响。
- `/api/version` 的 `name` 保留 `Quamm RES`（verify_v1.py 硬断言契约值）。
- `app/domain/prober_real.py` 未改动；端口 9100 不变；迁移只 `ALTER TABLE ADD COLUMN`（幂等），6 列全部可空、不强制填全。
- 冒烟新建的临时机型 `SMOKEQ*` 已从实时库清理（无设备/队列引用）。
