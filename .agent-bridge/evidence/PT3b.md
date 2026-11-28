---
task_id: PT3b
title: 设备独立详情页 #/device/{id}（指纹/能力/电源/串口/健康）+ 列表入口
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)
---

## a. 编译/构建

- 命令：`timeout 90s bash -lc '.venv/bin/python -m compileall -q app'`
  - 退出码：`0`
  - 关键结果：无输出（成功）
- 命令：`timeout 90s bash -lc '.venv/bin/python -m pytest tests/test_device_capabilities_api.py tests/test_powerres.py -q'`
  - 退出码：`0`
  - 关键结果：`18 passed, 3 warnings in 4.29s`（尾行）
- 命令：`timeout 120s bash -lc 'cd web && npm run build'`
  - 退出码：`0`
  - 关键结果：`✓ built in 3.99s`；产物 `web/dist/index.html` + `dist/assets/index-B_B8TbBj.js` / `index-D_g0xr8z.css` 存在

## b. 改动文件清单

| 路径 | 说明 | 行数 / sha256前16 / mtime |
|------|------|---------------------------|
| `app/api/router.py` | 新增 `GET /api/devices/{id}/capabilities` | 2356 / `e0a2b762cacebafd` / 2026-11-26 15:57:09 |
| `tests/test_device_capabilities_api.py` | **新建** enabled/disabled/404 单测 | 97 / `86daa7562feb7e69` / 2026-11-26 15:57:16 |
| `web/src/App.vue` | hash `#/device/{id}` + 详情页区块 + 列表/PowerRes 入口 | 6855 / `8170e0cb8b0066aa` / 2026-11-26 15:59:22 |
| `web/dist/assets/index-B_B8TbBj.js` | 构建产物 | — / `81880d9c6933f0e2` / 2026-11-26 15:59:39 |
| `web/dist/assets/index-D_g0xr8z.css` | 构建产物 | — / `d1dae305487c4493` / 2026-11-26 15:59:39 |
| `AGENT_BRIDGE_PROGRESS.md` | 本任务进度 | — |
| `AGENT_BRIDGE_EVIDENCE.md` | 本任务验收四要素 | — |

未改：bind/unbind 语义、COMRes、VERSION；未 commit/push；未重启服务。  
工作区另有 PT3a/PT3c 及更早未提交改动，本任务在其上继续。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`（摘要）：
  - `M` `app/api/router.py` `web/src/App.vue` `AGENT_BRIDGE_*`（及上游 PT3a/c 等）
  - `??` `tests/test_device_capabilities_api.py`（本任务新建）
  - 另有既有 `M`/`??`：capabilities/powerres/设计稿/证据镜像等（非本任务独占）
- `git diff --stat`（工作区整体）：`9 files changed, 2019 insertions(+), 105 deletions(-)`（含上游未提交；本任务主要增量：router +14、App.vue 详情页、新单测）
- Gerrit change：无/不适用
- 本任务未 commit

## d. 验证/自测

- `compileall` 退出码 0；`pytest … -q` → `18 passed` 退出码 0
- `npm run build` → `✓ built in 3.99s` 退出码 0
- 产物静态自查：`index-B_B8TbBj.js` 含 `device/`、`#/dashboard`、`返回设备中心`、`/capabilities`
- 手动可达性（逻辑/产物层；**未重启 :9100**）：
  - `#/device/{id}`：路由分支 `enterDevicePage` + 模板 `active==='device'` 已落地；存在设备拉详情/能力/串口/电源绑定
  - `#/device/nonexist`：`GET /api/devices/{id}` 404 → `el-empty`「设备不存在或已移除」，非白屏
  - `#/ctrlres` / `#/dashboard`：原 `resolveLegacyRoute` 分支保留，互不干扰
- 活服务冒烟：`curl :9100/api/devices/{id}/capabilities` → **404**（进程未加载本轮 router；预期，禁重启）；契约以 pytest TestClient 为准
