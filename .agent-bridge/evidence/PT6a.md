---
task_id: PT6a
title: 设备中心列表改为「电源能力」入口（截图移入设备页）+ TRES 版本号升到 0.54.0
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)，上游 PT3a/b/c/d + PT4a/b + PT5a 未提交改动之上
---

## a. 编译/构建

- `python -m compileall -q app`（`.venv/bin/python`）→ 退出码 0
- `.venv/bin/python -m pytest tests/test_powerres.py tests/test_device_capabilities_api.py tests/test_serial_flags.py tests/test_serial_openable.py tests/test_serial_power_guard.py -q` → 退出码 0，尾行 `48 passed, 14 warnings in 10.37s`（本任务新增 4：`test_serial_flags`×3 + `test_list_devices_includes_serial_power_n`×1）
- `cd web && npm run build` → 退出码 0，尾行 `✓ built in 4.32s`（chunk >500kB 仅告警，既有）
- 服务重启后 `curl -s http://127.0.0.1:9100/api/version` 应由主 Agent 验证；本任务已将 `VERSION` 改为 `0.54.0`

## b. 改动文件清单

| 路径 | 说明 |
|------|------|
| `app/domain/serial.py` | `serial_flags` 增 `power_n`（role=power 且 enabled）/ `log_n`（role=log） |
| `app/api/router.py` | `GET /devices` 惰性附 `serial` 标记；失败 try/except 不阻断 |
| `tests/test_serial_flags.py` | 新建：空表、power_n/log_n 计数、仅 log |
| `tests/test_device_capabilities_api.py` | 补 `GET /devices` 行含 `serial.power_n` |
| `web/src/App.vue` | 列表去截图/记录、加「电源」列+popover；详情页补「记录」；操作列宽 260→200 |
| `VERSION` | `0.53.0` → `0.54.0` |
| `CHANGELOG.md` | 顶部 `[0.54.0] - 2026-11-26` 本轮能力小节 |

**未改**：MCP 既有字段语义、bind 权限、`/devices` 其它字段；未 commit/push。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`：tracked 修改含 VERSION/CHANGELOG/serial/router/App.vue 等 + untracked（上游 PT3–PT5 + 本任务 `tests/test_serial_flags.py`），全部未 commit/push（按要求）
- `git diff --stat`（工作区 tracked 整体，含上游）：`13 files changed, 3011 insertions(+), 159 deletions(-)`
- `VERSION` 内容：`0.54.0`
- 本任务核心文件 sha256 前16：
  - `909e64fed63799e5` app/domain/serial.py
  - `5c0460565b631085` app/api/router.py
  - `6133eb2637e95f97` web/src/App.vue
  - `1eadf6791d9bd683` VERSION
  - `259b74bf6fe233fb` CHANGELOG.md
  - `493c1eafc009e0cd` tests/test_serial_flags.py
  - `b1c03ee7cc01d07b` tests/test_device_capabilities_api.py
- Gerrit change：不适用

## d. 验证/自测

- compileall 退出码 0
- pytest 目标组合退出码 0：`48 passed`（相对 PT5a 的 44 新增 ≥2）
- npm build 退出码 0：`dist/` 产物生成
- 权限未放宽：绑定仍仅 admin；列表电源动作沿用租约闸（非 admin 且非持租 → disabled + tooltip）
