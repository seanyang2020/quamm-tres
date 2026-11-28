---
task_id: PT1
title: PowerRes 页 404 降级 + overview 节点下拉 + 手工登记资源
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)
---

## a. 编译/构建

- 命令：`.venv/bin/python -m compileall -q app`
- 退出码：`0`
- 关键结果：通过（无输出）

- 命令：`.venv/bin/python -m pytest tests/test_powerres.py -q --tb=short`
- 退出码：`0`
- 关键结果：`10 passed in 2.58s`（含 overview / manual upsert / delete ≥3 新用例）

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：成功
  ```
  dist/index.html                     0.40 kB │ gzip:   0.29 kB
  dist/assets/index-DU3DB09y.css    372.64 kB │ gzip:  50.60 kB
  dist/assets/index-CHYIklaT.js   1,222.45 kB │ gzip: 389.43 kB
  ✓ built in 4.63s
  ```

## b. 改动文件清单

本任务（PT1）新增/修改：

| 路径 | 说明 | 行数（约） |
|------|------|-----------|
| `app/domain/powerres.py` | +overview / upsert_inventory_item / delete_inventory_item | 594 行（文件总） |
| `app/api/powerres_router.py` | +GET overview / POST inventory / DELETE inventory | 156 行（文件总） |
| `tests/test_powerres.py` | +3 用例（overview / upsert 幂等 / validation+delete） | 446 行（文件总） |
| `web/src/App.vue` | 404/405 降级、overview 下拉、手工添加对话框 | 相对 HEAD +547/−1（含 PV6b） |

未改 bind 语义 / COMRes / wirelog / capabilities；未改 VERSION；未动生产库；未重启服务。

工作区仍保留 PV6a/PV6b 未提交改动（未回滚）。

## c. 版本/提交证据

- `git log -1 --oneline` → `62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git branch` → `feat/capabilities`
- `git status --short`：
  ```
   M AGENT_BRIDGE_EVIDENCE.md
   M AGENT_BRIDGE_PROGRESS.md
   M app/domain/capabilities.py
   M app/main.py
   M app/migrations.py
   M app/models.py
   M web/src/App.vue
  ?? .agent-bridge/evidence/PV6a.md
  ?? .agent-bridge/evidence/t1029.md
  ?? app/api/powerres_router.py
  ?? app/domain/powerres.py
  ?? tests/test_powerres.py
  ```
- `git diff --stat`（已跟踪）：
  ```
   AGENT_BRIDGE_EVIDENCE.md   | 119 +++++++----
   AGENT_BRIDGE_PROGRESS.md   |  37 ++-
   app/domain/capabilities.py |  93 +++++++-
   app/main.py                |   2 +
   app/migrations.py          |  13 ++
   app/models.py              |  60 +++++
   web/src/App.vue            | 547 ++++++++++++++++++++++++++++++++++++++++++++-
   7 files changed, 802 insertions(+), 69 deletions(-)
  ```
- Gerrit：不适用
- **未 commit / 未 push**

### 文件指纹（sha256 前 16 + mtime）

| 路径 | sha16 | mtime |
|------|-------|-------|
| app/domain/powerres.py | 5e582149d23a2af1 | 2026-11-26 15:00:22 |
| app/api/powerres_router.py | 1503e8d15cb3afaf | 2026-11-26 15:00:29 |
| tests/test_powerres.py | f510e5cbe679fd7d | 2026-11-26 15:01:13 |
| web/src/App.vue | ce96e80e7661b125 | 2026-11-26 15:03:04 |

## d. 验证/自测

| 命令 | 退出码 | 结果 |
|------|--------|------|
| `.venv/bin/python -m compileall -q app` | 0 | 通过 |
| `.venv/bin/python -m pytest tests/test_powerres.py -q` | 0 | 10 passed in 2.58s |
| `cd web && npm run build` | 0 | built in 4.63s |

功能点（代码级）：
- A：inventory/overview 404/405 → `prUnsupported` 居中提示，无红条
- B：节点下拉改用 `GET /api/powerres/overview`；空时提示「该 TRES 没有节点上报电源资源」
- C：`POST/DELETE /api/powerres/inventory` + 前端「手工添加资源」对话框（节点选自 wlNodes 以支持尚无 inventory 的建档）
- D：bind/unbind 语义未改
