---
task_id: PT4b
title: 电源动作安全闸（hold 夹紧 + confirm + binding.enabled）
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)
---

## a. 编译/构建

- 命令：`.venv/bin/python -m compileall -q app` → 退出码 **0**
- 命令：`cd web && npm run build` → 退出码 **0**；产物 `web/dist/` 存在（vite build ✓ built in 4.09s）
- 关键结果：编译通过；前端构建通过

## b. 改动文件清单

| 路径 | 说明 |
|------|------|
| `app/domain/serial.py` | hold 夹紧 [500,30000]；power confirm；POWER_DISABLED；rejected 审计 |
| `app/api/router.py` | `/serial/action` 透传 `confirm` → `run_action`（CONFIRM_REQUIRED/POWER_DISABLED 经既有 `_domain_http`→400） |
| `app/mcp/server.py` | `res_serial_action` 透传 `confirm`；文档注明 power_off/cycle |
| `app/domain/powerres.py` | inventory 行补 `enabled`（来自 binding；只读展示用）；**bind 不接受 enabled，未改 bind 语义** |
| `web/src/App.vue` | hold max 30000；关/重启带 confirm；开不带；停用标签+禁用按钮；错误中文提示 |
| `tests/test_serial_power_guard.py` | **新建** ≥12 用例（hold 边界/confirm/enabled/无 binding/审计） |
| `tests/test_powerres.py` | 既有 power_cycle 用例补 `confirm=True` |
| `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md` | 本任务进度与证据 |

**前端启用切换**：`POST /api/powerres/bind` **不接受** `enabled`（始终写 True）→ 本期只读「停用」标签 + 禁用开/关/重启，**未加 PATCH/新 API**。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`：含本任务改动 + 上游 PT3 未提交（未 commit/push）
- `git diff --stat`（工作区整体 tracked）：`11 files changed, 2160 insertions(+), 109 deletions(-)`
- `git diff -- app/ web/ tests/ | sha256sum` 前 16：`bea9dfaeb82fa709`（业务改动指纹；全量 working tree 含 bridge 文档会漂移）
- Gerrit change：无/不适用
- 本任务未 commit / 未 push / 未重启服务

## d. 验证/自测

- `.venv/bin/python -m compileall -q app` → exit 0
- `.venv/bin/python -m pytest tests/test_powerres.py tests/test_device_capabilities_api.py tests/test_serial*.py -q` → **40 passed**，exit 0（其中 `test_serial_power_guard.py` **12 passed**）
- `cd web && npm run build` → exit 0
- 口径核对：`CONFIRM_REQUIRED`/`POWER_DISABLED` 走 `_domain_http` 默认分支 → HTTP 400 + `detail.code`
