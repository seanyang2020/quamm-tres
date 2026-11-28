---
task_id: PV6a
title: TRES 新增 PowerRes 资源域（后端 + 能力描述符接线）
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
- 关键结果：`7 passed in 1.82s`

- 命令：`.venv/bin/python -m pytest tests/test_capabilities.py tests/test_powerres.py -q --tb=line`
- 退出码：`0`
- 关键结果：`27 passed in 7.59s`

## b. 改动文件清单

- `app/models.py`（+PowerResBinding / PowerResInventory）
- `app/domain/powerres.py`（新增）
- `app/api/powerres_router.py`（新增）
- `app/domain/capabilities.py`（PowerRes endpoint 匹配，仅影响 power item）
- `app/main.py`（include powerres_router）
- `app/migrations.py`（`_ensure_powerres_tables` 幂等补表）
- `tests/test_powerres.py`（新增）
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md`（本任务证据）

未改：前端 web、`WireLogPortBinding` 唯一键、`/api/comres/*`、生产库。

## c. 版本/提交证据

- `git log -1 --oneline` → `62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git branch` → `feat/capabilities`
- `git status --short`：本域未提交改动（见上清单）；另有无关 `?? .agent-bridge/evidence/t1029.md`
- `git diff --stat`（已跟踪文件）：
  ```
   app/domain/capabilities.py | 93 +++++++++++++++++++++++++++++++++++++++++++---
   app/main.py                |  2 +
   app/migrations.py          | 13 +++++++
   app/models.py              | 60 ++++++++++++++++++++++++++++++
   4 files changed, 162 insertions(+), 6 deletions(-)
  ```
  另有未跟踪：`app/domain/powerres.py`、`app/api/powerres_router.py`、`tests/test_powerres.py`
- Gerrit：不适用
- **未 commit / 未 push**

### 文件指纹（sha256 前 16 + mtime）

| 路径 | sha16 | mtime |
|------|-------|-------|
| app/models.py | 352f0bfaffa565f3 | 2026-11-26 13:50:06 +0800 |
| app/domain/powerres.py | 65b5e8e9a5951b26 | 2026-11-26 13:50:52 +0800 |
| app/api/powerres_router.py | 0cae34858e3ac485 | 2026-11-26 13:50:49 +0800 |
| app/domain/capabilities.py | 3bdb19ebce424ab2 | 2026-11-26 13:51:30 +0800 |
| app/main.py | b7cfb4c37b4d7b71 | 2026-11-26 13:50:54 +0800 |
| app/migrations.py | 776fb17a28d1986b | 2026-11-26 13:50:56 +0800 |
| tests/test_powerres.py | 24727522d6f11623 | 2026-11-26 13:51:54 +0800 |

## d. 验证/自测

- `pytest tests/test_powerres.py -q` → **7 passed**（sync 幂等/清理、bind 建 port、复用 slot、unbind、slot 用尽、capabilities 清 NO_BINDING、空 endpoint）
- `pytest tests -q` → **17 failed, 243 passed**（约 62s）
  - 既有失败项（与本改动无关）：`test_apply_flow`（产品种子）、`test_flash.*`、`test_mcp_http_full_flow` / `list_products_never_empty`、`test_queue`、`test_registration`（mock probe / 产品列表）
  - 本域新增用例均在 passed 侧；未新增与 PowerRes/capabilities 相关的失败

未做：未 commit、未 push、未部署、未写 172.16.1.201 生产库
