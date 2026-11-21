---
task_id: t5-comres-offline-allocated
---

## a. 编译/构建

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/domain/comres.py tests/smoke_comres.py`  
退出码：`0`  
结果：无语法错误；产物为源码域层（无独立二进制）。

## b. 改动文件清单

- `app/domain/comres.py`
- `tests/smoke_comres.py`
- `.agent-bridge/evidence/t5-comres-offline-allocated.md`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`

未改：`app/api/comres_router.py`、`/api/wirelog`、deskserialhub。

## c. 版本/提交证据

见本轮 commit 后 `git log -1 --oneline` / `git status --short` / `git diff --stat`（提交完成后回填）。

Gerrit：不适用。

## d. 验证/自测

1. `PYTHONPATH=. .venv/bin/python tests/smoke_comres.py` → `RESULT ALL_PASS` / exit `0`  
   - ACCEPT-9 未分配离线语义保持  
   - ACCEPT-10：已分配未报仍 online + allocated 视图；释放后再未报 → offline；再上报恢复 probe
