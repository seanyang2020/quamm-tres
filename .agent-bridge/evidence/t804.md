---
task_id: t804
---

# Quamm TRES v0.23.0 验收证据（task_id: t804）

> 新增「设备截图 + 芯片/浮点能力」并经 MCP 释放。未 git commit / 未 push，停住等审。
> 说明：仓库既有未提交改动 `app/discovery.py` 及 4 个文件的部分改动为用户在途工作，本任务未触碰/未回滚；
> 两个 `*.bak-caddyadd` 文件为既有未跟踪文件，明确不纳入提交。

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `timeout 90 .venv/bin/python -m py_compile app/screenshot.py app/db_source.py app/models.py app/migrations.py app/domain/catalog.py app/domain/prober_real.py app/api/router.py app/mcp/server.py scripts/verify_screenshot.py tests/test_screenshot.py` | 0 | 10 个文件全部编译通过 |
| 全模块导入 | `timeout 60 .venv/bin/python -c "import app.main, app.api.router, app.mcp.server, app.screenshot, ...; get_version()"` | 0 | 导入无循环/无异常，`get_version()=0.23.0` |
| 前端构建 | `cd web && timeout 240 npm run build` | 0 | `web/dist/index.html` + `assets/index-zGme4AFT.js`(1.10MB) + `assets/index-DH4OyeMW.css`(364KB) 重建成功 |
| 新增单测 | `timeout 90 .venv/bin/python -m pytest tests/test_screenshot.py -q` | 0 | `27 passed in 0.03s` |
| 全量回归 | `timeout 180 .venv/bin/python -m pytest tests/ -q` | 1 | `17 failed, 57 passed`；17 项为**仓库既存失败**（见下方对照，与本任务无关），无新增回归 |
| 迁移幂等（临时库） | `timeout 60 .venv/bin/python -c "from app.migrations import ...; migrate(s)"`（老 schema 两次） | 0 | 首跑补 3 列、既有行默认 `('auto', 0)`；二次幂等不报错 |
| MCP 工具数 | `timeout 60 .venv/bin/python -c "asyncio.run(mcp.list_tools())"` | 0 | 30 → **31**，`res_screenshot` 已注册，参数 `device_id/ip/port/quality/rotate` |

全量回归对照（证明 17 项失败为既存、无回归）：对干净 HEAD 建临时 worktree
`git worktree add /tmp/quamm-head-clean HEAD` 后跑同一命令 → `17 failed, 30 passed`；
`diff` 两份 `FAILED` 名单为空（完全一致）；工作树多出的 27 passed 即本任务新增 27 项单测。
既存失败根因：v0.17 起 `seed.PRODUCT_TYPES=[]`，部分旧测试仍断言 5 个机型 / flash 依赖，
与本任务改动无关，未擅自改动测试或种子。

## b. 改动文件清单

修改：
- `VERSION`（0.22.0 → 0.23.0）
- `CHANGELOG.md`（增 [0.23.0] - 2026-11-09）
- `app/screenshot.py`（新增，见下）
- `app/vendor/xosdbg`、`app/vendor/vxosdbg`（新增二进制，见下）
- `app/domain/prober_real.py`（读 /proc/cpuinfo，extra 增 4 字段）
- `app/models.py`（ProductType 增 chip_model / float_abi / has_fpu）
- `app/migrations.py`（`_PRODUCT_TYPE_CHIP_COLUMNS` + 幂等 `_ensure_columns`）
- `app/domain/catalog.py`（list_products 暴露 3 字段；`_backfill_product_chip` 注册自动回填）
- `app/api/router.py`（品类增/改 3 字段；POST 截图；GET 图片防穿越）
- `app/mcp/server.py`（新增 `res_screenshot`，30→31；导入 screenshot；docstring 计数）
- `app/db_source.py`（`XOSADB2UPDATE_PATH` 可配置常量，默认 update_args 不变）
- `web/src/App.vue`（设备表 admin 截图按钮 + el-image 预览；品类表单/表格加芯片·浮点字段）
- `scripts/verify_screenshot.py`（新增最小验证脚本）
- `tests/test_screenshot.py`（新增 27 项纯函数单测）
- `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md`（本任务进度与证据）

新增：
- `app/screenshot.py`（标准库 + subprocess；浮点 ABI 三级判定 + adb 截图全流程）
- `app/vendor/xosdbg`（软浮点 eabi，343888 B，sha256 与源一致，可执行）
- `app/vendor/vxosdbg`（硬浮点 eabihf，368640 B，sha256 与源一致，可执行）
- `scripts/verify_screenshot.py`
- `tests/test_screenshot.py`

未触碰 / 不纳入提交：
- `app/discovery.py`（用户既存未提交改动，本任务未改）
- `web/src/App.vue.bak-caddyadd`、`web/vite.config.js.bak-caddyadd`（既存未跟踪，明确不提交）
- `web/dist.bak-caddyadd/`（gitignore `*.bak-caddyadd/`）
- `app/var/screenshots/*.jpg`（运行产物，gitignore `var/`，不提交）

建议显式提交清单（供提交时逐项列出，勿用 `git add -A`）：
`VERSION CHANGELOG.md app/screenshot.py app/vendor/xosdbg app/vendor/vxosdbg app/domain/prober_real.py app/models.py app/migrations.py app/domain/catalog.py app/api/router.py app/mcp/server.py app/db_source.py web/src/App.vue scripts/verify_screenshot.py tests/test_screenshot.py AGENT_BRIDGE_PROGRESS.md AGENT_BRIDGE_EVIDENCE.md`

## c. 版本/提交证据

```
$ git log -1 --oneline
8c5f1a4 v0.22.0: 设备页两级重构，去未分类，概况+品类卡片+详情状态tab

$ git status --short
 M CHANGELOG.md
 M VERSION
 M app/api/router.py
 M app/db_source.py
 M app/discovery.py            # 用户既存未提交改动，本任务未触碰
 M app/domain/catalog.py
 M app/domain/prober_real.py
 M app/mcp/server.py
 M app/migrations.py
 M app/models.py
 M web/src/App.vue
?? app/screenshot.py
?? app/vendor/
?? scripts/verify_screenshot.py
?? tests/test_screenshot.py
?? web/src/App.vue.bak-caddyadd        # 不提交
?? web/vite.config.js.bak-caddyadd     # 不提交

$ git diff --stat
 CHANGELOG.md              | 33 +++++++++++++++++
 VERSION                   |  2 +-
 app/api/router.py         | 67 ++++++++++++++++++++++++++++++++--
 app/db_source.py          | 10 +++++-
 app/discovery.py          | 27 +++++++++++++-      # 用户既存
 app/domain/catalog.py     | 45 ++++++++++++++++++++++-
 app/domain/prober_real.py | 85 +++++++++++++++++++++++++++++++++----------
 app/mcp/server.py         | 35 ++++++++++++++++--
 app/migrations.py         | 12 +++++++
 app/models.py             |  9 +++++
 web/src/App.vue           |  92 +++++++++++++++++++++++++++++++++++++++++++++--
 11 files changed, 388 insertions(+), 29 deletions(-)
```

- Gerrit change：无/不适用
- 本任务本地改动尚未 `git commit`；未 push origin（按要求由用户提交推送）
- 未重启 uvicorn/worker：新 REST/MCP 端点需进程重启后生效；本次验证均通过「直接调用处理函数 + 全新进程导入」完成

## d. 验证/自测

| 功能 | 命令/方式 | 结果 |
|---|---|---|
| 真实设备截图（要求项） | `timeout 150 .venv/bin/python scripts/verify_screenshot.py` | exit 0；`PASS`：ip=172.16.10.246，`float_abi=hard`(source=cpuinfo)、`bin=vxosdbg`、pull 回 `app/var/screenshots/172.16.10.246_20261109T061947689148Z.jpg`，`有效 JPEG（7203 bytes，240x320，SOI/EOI/SOF 完整）` |
| jpg 可打开 | `timeout 15 file app/var/screenshots/<...>.jpg` | `JPEG image data, JFIF standard 1.01, ... baseline, precision 8, 240x320, components 3` |
| /proc/cpuinfo 实测 | `timeout 15 adb -s 172.16.10.246:5555 shell "cat /proc/cpuinfo"` | Features 含 `vfp/neon/vfpv3/vfpv4`；CPU part `0xc07`、implementer `0x41` |
| prober_real 新 extra | `timeout 60 .venv/bin/python -c "RealXosProber().probe(...)"` | L3/qm10xv_full；extra 含 `features/cpu_part=0xc07/cpu_implementer=0x41/has_fpu=True` |
| 二进制 ABI 实测 | push 到 /tmp 后执行 `--version` | vxosdbg(hard) `version 2.0.1` rc=0；xosdbg(soft) 在本 FPU 机上亦 rc=0（故以 cpuinfo 为准，另加截图时 ABI 不匹配自动换另一个的兜底） |
| xosscreen 抓图 | `/tmp/xosdbg xosscreen -o ... -q 60 -r 0 -d /dev/fb0 -s` | rc=0，设备端生成 240x320 jpg；`-s/--skipcompare` 跳过「未变化则不抓图」优化，保证每次产出 |
| /data 不可用降级 | 设备 `df`：`/data` 448K、`/tmp` 13.5M | `/data` push 因空间不足失败 → 自动退 `/tmp/xosdbg` 成功（流程内 4 组合遍历） |
| MCP res_screenshot(device_id) | `set_auth(...) + res_screenshot(device_id='dev_fd5bf74ddf', quality=55)` | 返回 `device_id/ip/float_abi=hard/bin=vxosdbg/file_name/bytes=6644/...`，DomainError→`_err` 包装 |
| REST POST 处理函数 | `device_screenshot('dev_fd5bf74ddf', {...})` 直调 | 返回 `file_name/bytes/float_abi=hard/bin=vxosdbg` |
| REST GET + 防穿越 | `get_screenshot(<合法名>)` 与 4 个恶意文件名 | 合法名 → `FileResponse(media_type=image/jpeg)`；`../quamm_res.db`、`..%2fetc`、`a/b.jpg`、`evil.png` 均 400 拦截 |
| 芯片/浮点自动回填 | 临时库 `_backfill_product_chip(...)` | `auto/0/None` → `hard/True/Cortex-A7`；人工设 `soft/ManualChip` 不被覆盖；`list_products` 暴露 3 字段 |
| 迁移幂等 | 老 schema 临时库跑 `migrate()` 两次 | 补 3 列、既有行默认 `('auto',0)`、二次不报错；并已对本地库幂等补列（`qm10xv_full` 现为 `auto/0/NULL`） |
| db_source 默认 | `XOSADB2UPDATE_PATH` / `which` / `upgrade.__defaults__` | `/home/scm/bin/xosadb2update` 存在；默认解析到该路径；默认 `update_args='-f -a -e data'`（保留 data 区）未变 |
| 前端 | `cd web && npm run build` | exit 0，dist 重建（chunk 体积告警为既存、非错误） |

### 备注
- 所有 adb/设备/远程命令均用 `timeout` 包裹（单条 `timeout 15` + subprocess `timeout+2` 双保险）。
- 本机 adb 不回传远端 shell 退出码，故设备端命令尾部统一追加 `; echo __QUAMM_RC__=$?` 解析真实 rc。
- 未重启在跑的 uvicorn/worker；未写入 mock 业务数据；未 push。
