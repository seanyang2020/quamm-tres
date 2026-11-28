---
task_id: PT2
title: TRES 前端 COMRes/PowerRes 收拢为 CtrlRes + hash 深链
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)
---

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：成功
  ```
  dist/index.html                     0.40 kB │ gzip:   0.29 kB
  dist/assets/index-DU3DB09y.css    372.64 kB │ gzip:  50.60 kB
  dist/assets/index-D4xgpe9Z.js   1,223.65 kB │ gzip: 389.81 kB
  ✓ built in 4.97s
  ```
- 产物（gitignore `dist/`，本地存在）：
  - `web/dist/assets/index-D4xgpe9Z.js` sha256前16=`d56cb956caaaef15` mtime=`2026-11-26 15:08:03 +0800`
  - `web/src/App.vue` sha256前16=`8b603d86cfb7eed6` mtime=`2026-11-26 15:07:41 +0800`

## b. 改动文件清单

本任务（PT2）仅前端（在 PT1 工作区结果上继续，未回滚）：

| 路径 | 说明 |
|------|------|
| `web/src/App.vue` | pages→CtrlRes；两卡片 el-tabs；hash 深链/旧 hash 兼容；switchPage/boot/jumpToWlNode 同步 |
| `web/dist/**` | `npm run build` 产物（gitignore，未入库） |

未改：`app/` 后端、VERSION、其他页面行为、依赖；未 commit/push；未重启服务。

**结构选型**：页内 `el-tabs`（非并列 el-card）。理由：与 `#/ctrlres?card=` 深链一一对应；COMRes 内部已多层 el-card，再套卡片会双重嵌套；默认 tab=COMRes。

**grep 残留说明**（`grep -nE "'wirelog'|'powerres'" web/src/App.vue`）：

| 行 | 用途 |
|----|------|
| 1133 | hash 同步：`card=powerres` |
| 1160/1163 | 切卡片：card key `powerres` |
| 1191–1194/1200 | 旧 hash 兼容 `#/wirelog`/`#/powerres` + `?card=powerres` |
| 2668–2669 | switchPage 旧 key 映射 `wirelog`/`powerres`→`ctrlres` |
| 2684 | ctrlres 内按 card key 拉数 |

无页面级 `active === 'wirelog'|'powerres'` 残留（已确认 `NO_PAGE_LEVEL_ACTIVE`）。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`（本 cwd 含 PT1 未提交后端 + 本任务前端）：
  ```
  M AGENT_BRIDGE_EVIDENCE.md
  M AGENT_BRIDGE_PROGRESS.md
  M app/domain/capabilities.py
  M app/main.py
  M app/migrations.py
  M app/models.py
  M web/src/App.vue
  ?? .agent-bridge/evidence/...
  ?? app/api/powerres_router.py
  ?? app/domain/powerres.py
  ?? tests/test_powerres.py
  ```
- `git diff --stat`（全工作区，含 PT1）：
  ```
  AGENT_BRIDGE_EVIDENCE.md   | 123 ++++++-
  AGENT_BRIDGE_PROGRESS.md   |  35 +++-
  app/domain/capabilities.py |  93 ++++++-
  app/main.py                |   2 +
  app/migrations.py          |  13 +
  app/models.py              |  60 ++++
  web/src/App.vue            | 663 +++++++++++++++++++++++++++++++++++++++++++--
  7 files changed, 897 insertions(+), 92 deletions(-)
  ```
- 本任务前端：`web/src/App.vue` `+639/-24`（相对 HEAD；含 PT1 前端与 PT2 收拢）
- Gerrit：无/不适用
- 未 commit / 未 push

## d. 验证/自测

- `cd web && npm run build` → 退出码 `0`（见 a）
- `grep -nE "active === 'wirelog'|active === 'powerres'|active\.value = 'wirelog'|active\.value = 'powerres'" web/src/App.vue` → 无匹配（`NO_PAGE_LEVEL_ACTIVE`）
- `grep -nE "'wirelog'|'powerres'" web/src/App.vue` → 仅 hash/卡片 key/旧 key 映射（见 b 表）
- 引用点同步清单：
  - `pages[]`：`wirelog`+`powerres` → `ctrlres`
  - `ctrlresCard` + `syncCtrlresHash` / `resolveLegacyRoute` / `hashchange`
  - `enterComRes` / `enterPowerRes` / `onCtrlresCardChange` / `loadComresForCurrentTab`
  - `switchPage`：`ctrlres` 分支 + 旧 key 兼容
  - `jumpToWlNode` → `ctrlres`+`comres`
  - 概览「串口资源」→ 强制 `comres` 后 `switchPage('ctrlres')`
  - `boot`：深链优先，未命中则设备中心
