---
task_id: t792
---

# Quamm TRES v0.14 验收证据（task_id: t792）

> MCP 固件下载/刷机 + semver 版本策略 + 保留 data 分区。本地 commit `e127831`，未 push。

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `timeout 60 .venv/bin/python -m py_compile app/config.py app/models.py app/migrations.py app/domain/flash.py app/domain/leasing.py app/domain/prober_real.py app/domain/patrol.py app/mcp/server.py` | 0 | 8 个文件编译通过（含新建 `flash.py`） |
| 单测 | `timeout 400 .venv/bin/python -m pytest tests/ -q` | 0 | `43 passed in 22.38s`（原 33 + 新增 `tests/test_flash.py` 10 项） |
| 实时库迁移 | `timeout 90 .venv/bin/python -c "configure(...); init_db(); migrate(s)"` | 0 | `migrate ok`；flash_job 表创建；product_type/device 新列存在 |
| 服务重启 | kill 988126/988194 → `setsid nohup` 启 uvicorn+worker | 0 | uvicorn 991224、worker 991225；`/api/health` 200 `{"status":"ok","version":"0.14.0"}` |

## b. 改动文件清单

- `app/config.py`（XOS base url / dbxostest 路径 / 日志与工作目录 / 超时）
- `app/models.py`（FlashJob 新表；ProductType.db_project_name/required_major/minor/patch；Device.firmware_version）
- `app/migrations.py`（幂等加列；5 个机型 db_project_name 空值回填）
- `app/domain/flash.py`（新增：版本目录解析、semver、异步 job 状态机、日志、固件状态）
- `app/domain/leasing.py`（res_apply 版本门禁：NO_COMPATIBLE_DEVICE / UNKNOWN_VERSION / patch hint）
- `app/domain/prober_real.py`（读 `/etc/fw_version`，回退 `/etc/version`；`read_firmware_version`）
- `app/domain/patrol.py`（巡检回填 device.firmware_version，3 行）
- `app/mcp/server.py`（4 个 MCP 工具；工具数 17→21）
- `tests/test_flash.py`（新增 10 项）
- `VERSION` 0.13.0 → 0.14.0；`CHANGELOG.md` 增 [0.14.0]；`.gitignore` 增 `var/`
- 未动前端 Vue / `web/`；`web/*.bak-caddyadd` 保持未跟踪

## c. 版本/提交证据

```
$ git log -1 --oneline
e127831 feat(v0.14): MCP firmware download/flash + semver policy + preserve data (t792)

$ git status --short
 M web/vite.config.js              ← 非本任务改动（23:24 外部写入，疑似 t789 后续），未触碰
?? web/src/App.vue.bak-caddyadd
?? web/vite.config.js.bak-caddyadd
（仅上述前端遗留；本任务代码/测试/文档均已提交）

$ git diff --stat HEAD~1 HEAD
 .gitignore                |   3 +
 CHANGELOG.md              |  14 +
 VERSION                   |   2 +-
 app/config.py             |   9 +
 app/domain/flash.py       | 523 ++++++++++++++++++++++++++++++++++
 app/domain/leasing.py     |  70 ++++++--
 app/domain/patrol.py      |   4 +
 app/domain/prober_real.py |  50 ++++-
 app/mcp/server.py         |  50 ++++-
 app/migrations.py         |  38 ++++
 app/models.py             |  41 ++++
 tests/test_flash.py       | 331 ++++++++++++++++++++++
 12 files changed, 1123 insertions(+), 12 deletions(-)
```

- Gerrit change：无/不适用（远端为 GitHub SSH，无 Gerrit）。
- 基线：`65b11e8`（v0.13.0 t791）；本任务本地提交 `e127831`（代码/测试）+ `509f8a9`、`ade131a`（进度与证据），未 push。

## d. 验证/自测

| 项 | 命令 / 操作 | 结果 |
|---|---|---|
| 单测 | `timeout 400 .venv/bin/python -m pytest tests/ -q` | 43 passed；覆盖：成功/失败/清空 data 确认/同租约互斥/他人租约拒绝/版本列表排序/不可达/无工程/门禁 |
| MCP 工具数 | `tools/list`（HTTP JSON-RPC，临时凭据） | 21 个；含 `res_firmware_versions` / `res_flash` / `res_flash_status` / `res_firmware_status` |
| 版本目录 | MCP `res_firmware_versions` ×5 机型 | esl2 369、framex 922、moto_ld 154、qm10xh_full 236、qm10xv_full 130（均按时间倒序） |
| 版本门禁 | MCP `res_apply`（moto_ld，fw 为空） | GRANTED + `firmware_warning=UNKNOWN_VERSION` |
| 真实刷机 | MCP `res_flash(lease_id=L-72569dbde9, version=154, preserve_data=true)` | job `fj_6241faa6a063`：QUEUED→RUNNING→SUCCEEDED；日志含 `xosadb2update -f -a -e data`、`✓ 更新命令执行成功` |
| 首次失败留证 | 同上（修正前） | job `fj_16a108151b48` FAILED：`argument -u/--update-args: expected one argument`；改用 `--update-args=` 后成功 |
| 刷机后固件 | `res_firmware_status(dev_dfbff92075)` | `current_version=154`；required 均 0；`upgrade_needed=false`（版本串为纯 build 号，按 UNKNOWN_VERSION 展示） |
| 设备回读 | 刷机后线程 adb 回读 `/etc/fw_version` | 设备重启期间 `No route to host`，回读失败，按约定落刚刷版本号 `154`；约 15 分钟后仍不可达 |
| 目标巡检 | `run_patrol_round(session, ['dev_dfbff92075'])` | L0、fails=1，health=L0（移出可申请池） |
| 服务 | `/api/health`、`/api/version` | 200；`0.14.0`，name `Quamm TRES` |

### 备注

- 保留 data：真实任务 `-u` 透传为 `-f -a -e data`，未出现 `-e` 缺失；erase 分支单测验证不含 `-e data`。
- 设备未恢复网络属真实刷机常见结果，任务允许「中途失败也接受」；未再对设备做任何写操作。
