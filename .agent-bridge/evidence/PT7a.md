---
task_id: PT7a
title: 管理员可直接操作未绑定通道（通道级动作 API）
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)，上游 PT3–PT6 未提交改动之上
---

## a. 编译/构建

- `python -m compileall -q app`（`.venv/bin/python`）→ 退出码 0
- `.venv/bin/python -m pytest tests/test_powerres.py tests/test_powerres_action.py tests/test_serial*.py -q` → 退出码 0，尾行 `55 passed, 12 warnings in 13.72s`（本任务新增 `test_powerres_action.py` ≥8 例）
- `cd web && npm run build` → 退出码 0，尾行 `✓ built in 5.28s`（chunk >500kB 仅告警，既有）

## b. 改动文件清单

| 路径 | 说明 |
|------|------|
| `app/domain/powerres.py` | 新增 `channel_action`：帧构造/占用键/hold 夹紧/enabled 闸/inventory 回写/审计 |
| `app/api/powerres_router.py` | `POST /action`（`require_admin`）；`_domain_http` 映射 UNKNOWN→404、BUSY→409 |
| `web/src/App.vue` | 未绑定 admin 可开/关/重启；admin 走通道级 API；查询按钮；`POWERRES_BUSY`/`POWERRES_NO_ENDPOINT` 中文提示 |
| `tests/test_powerres_action.py` | 新建：帧/cycle 顺序/hold 夹紧/未绑+停用/未知+空端点/403+admin/query |
| `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md` | 本任务证据与进度 |

**未改**：`serial.check_access`、bind/unbind 语义、设备级动作接口、VERSION；未 commit/push；未重启服务。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`：tracked 修改含 App.vue 等 + untracked（`app/domain/powerres.py` / `app/api/powerres_router.py` / `tests/test_powerres_action.py` 等，上游 PT3–PT6 + 本任务），全部未 commit/push（按要求）
- `git diff --stat`（工作区 tracked 整体，含上游）：`13 files changed, 3134 insertions(+), 158 deletions(-)`
- `VERSION` 内容：`0.54.0`（本任务未改）
- 本任务核心文件 sha256 前16：
  - `5f3bb2912999319a` app/domain/powerres.py
  - `12606167125490a9` app/api/powerres_router.py
  - `87472d9f44e5e792` web/src/App.vue
  - `0d6c474f4b6580d6` tests/test_powerres_action.py
- Gerrit change：不适用

## d. 验证/自测

- compileall 退出码 0
- pytest 目标组合退出码 0：`55 passed`（含 `tests/test_powerres_action.py`）
- npm build 退出码 0：`dist/` 产物生成
- 行为：admin 未绑定通道可 `POST /api/powerres/action`；已绑 `enabled=False` → `POWER_DISABLED`；非 admin → 403
