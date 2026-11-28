---
task_id: PT3c
title: PowerRes 绑定对话框优化（机型过滤 + 关键字搜索 + 可绑优先 + 已绑数）
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)
---

## a. 编译/构建

- 命令：`timeout 120s bash -lc 'cd web && npm run build'`
  - 退出码：`0`
  - 关键结果：`✓ built in 4.13s`；产物 `web/dist/index.html` + `dist/assets/index-*.js/css` 存在
  - 尾行：`✓ built in 4.13s`（另有 chunk size warning，非失败）

## b. 改动文件清单

| 路径 | 说明 | 行数 / sha256前16 / mtime |
|------|------|---------------------------|
| `web/src/powerresBindTargets.js` | **新建**纯函数：`filterBindTargets` 等 | 167 / `a394c891f4b5bbc0` / 2026-11-26 15:52:18 |
| `web/src/App.vue` | 绑定对话框：机型+关键字+排序+bindings 已绑数 | 6120 / `0622bfe9518913ad` / 2026-11-26 15:53:10 |
| `AGENT_BRIDGE_PROGRESS.md` | 本任务进度 | — |
| `AGENT_BRIDGE_EVIDENCE.md` | 本任务验收四要素 | — |

未改：后端 / bind 语义 / 其他页面 / VERSION；未 commit/push；未重启服务。  
工作区另有 PT3a 及更早未提交改动，本任务在其上继续（只增绑定对话框前端能力）。

**纯函数位置与行为（无前端测试框架，跳过单测）：**

- 文件：`web/src/powerresBindTargets.js`
- `filterBindTargets(devices, {model, keyword, slotUsed, powerBound, slotMax})`：排除 retired/disabled → 机型等值 → 关键字 includes → label 格式化 → 有空闲槽优先排序
- `collectModelClasses(devices)`：非空 model_class 去重排序
- `aggregatePowerBoundCounts(bindings)`：按 device_id 计数
- `SLOT_MAX = 16`

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`（摘要）：
  - `M` `web/src/App.vue` `AGENT_BRIDGE_*`（及上游 PT3a 等既有改动）
  - `??` `web/src/powerresBindTargets.js`（本任务新建）
  - 另有既有 `M`/`??`：capabilities/models/migrations/serial_tcp/powerres*/设计稿/证据镜像等（非本任务独占）
- `git diff --stat`（工作区整体）：`8 files changed, 1267 insertions(+), 103 deletions(-)`（含上游未提交；本任务主要增量在 `App.vue` 绑定对话框 + 新模块）
- Gerrit change：无/不适用
- 本任务未 commit

## d. 验证/自测

- 命令：`timeout 120s bash -lc 'cd web && npm run build'`
  - 退出码：`0`
  - 结果：Vite production build 成功（见 a）
- 命令：`node --input-type=module` 冒烟 `powerresBindTargets.js`
  - 退出码：`0`
  - 结果：retired 排除；keyword=`ALICE` 命中；BM88 机型过滤；满槽置底且 label 含「（无空闲槽）」；powerBound 聚合正确
- 前端单测框架：无（`package.json` 仅 vite/vue/element-plus）→ 跳过正式单测
