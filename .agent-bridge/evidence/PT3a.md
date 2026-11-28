---
task_id: PT3a
title: PowerRes 卡片直接控制（开/关/定时重启）+ steps 字段修正
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)
---

## a. 编译/构建

- 命令：`timeout 60s .venv/bin/python -m compileall -q app`
  - 退出码：`0`
  - 关键结果：通过
- 命令：`timeout 120s bash -lc 'cd web && npm run build'`
  - 退出码：`0`
  - 关键结果：`✓ built in 4.46s`；产物 `web/dist/index.html` + `dist/assets/index-*.js/css` 存在

## b. 改动文件清单

| 路径 | 说明 | 行数(约) / sha256前16 / mtime |
|------|------|-------------------------------|
| `app/domain/powerres.py` | 默认 steps→data/encoding；补空 steps power_cycle | 610 / `1ee89d65f933aef1` / 2026-11-26 15:44:44 |
| `app/serial_tcp.py` | normalize_steps hex→data 读兼容 + debug | 530 / `c919385701498252` / 2026-11-26 15:44:55 |
| `app/migrations.py` | `migrate_serial_action_hex_steps`（不自动跑） | 610 / `7269782ea8fee32b` / 2026-11-26 15:45:07 |
| `tests/test_powerres.py` | 新增 ≥4 用例（steps/hex/cycle/迁移） | 663 / `5a31d01313da8bb8` / 2026-11-26 15:45:50 |
| `web/src/App.vue` | PowerRes 卡片开/关/重启+hold_ms+补偿开 | 6028 / `1a80c7036dc85424` / 2026-11-26 15:47:41 |
| `AGENT_BRIDGE_PROGRESS.md` | 本任务进度 | — |
| `AGENT_BRIDGE_EVIDENCE.md` | 本任务验收四要素 | — |

未改：COMRes/设备中心其他行为、bind/unbind 语义、VERSION；未 commit/push；未重启服务。  
工作区另有既有未提交（PV6a/PT1/PT2 等），本任务在其上继续。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`（摘要）：
  - `M` `app/serial_tcp.py` `app/migrations.py` `web/src/App.vue` `AGENT_BRIDGE_*`
  - `??` `app/domain/powerres.py` `tests/test_powerres.py`（上游 PT1/PT2 起未跟踪；本任务继续改）
  - 另有既有 `M`/`??`：capabilities/models/main/powerres_router/设计稿/证据镜像等（非本任务独占）
- `git diff --stat`（工作区整体）：`8 files changed, 1149 insertions(+), 97 deletions(-)`（含上游未提交改动）
- Gerrit change：无/不适用
- 本任务未 commit

## d. 验证/自测

- 命令：`timeout 120s .venv/bin/python -m pytest tests/test_powerres.py tests/test_serial*.py -q`
  - 退出码：`0`
  - 结果：`24 passed, 1 warning in 6.20s`
  - 新增用例：`test_bind_default_actions_use_data_encoding`、`test_legacy_hex_steps_still_execute`、`test_power_cycle_hold_ms_trail`、`test_migrate_serial_action_hex_steps_idempotent`、`test_normalize_steps_hex_compat_both_fields`
- 手工迁移（**未对生产库执行**）：见 `migrate_serial_action_hex_steps` docstring / PROGRESS
