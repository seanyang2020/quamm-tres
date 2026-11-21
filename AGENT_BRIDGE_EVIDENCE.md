---
task_id: t948
---

## a. 编译/构建

命令：`cd web && npm run build`  
退出码：`0`  
结果：`✓ built in 5.23s`；产物 `web/dist/index.html`、`web/dist/assets/index-DuOAhWKt.js` 存在（dist 不入库）。

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/domain/comres.py app/domain/wirelog.py app/domain/comres_prune.py`  
退出码：`0`

命令：`PYTHONPATH=. .venv/bin/python -m pytest tests/test_comres_dedupe.py tests/test_comres_bound.py tests/test_comres_prune.py tests/test_comres_apply.py -q`  
退出码：`0`  
结果：`22 passed`

## b. 改动文件清单

- `app/api/comres_router.py`
- `app/api/wirelog_router.py`
- `app/config.py`
- `app/domain/comres.py`（F6 auto→bound；F5 端点接管）
- `app/domain/comres_apply.py`
- `app/domain/comres_prune.py`（新）
- `app/domain/wirelog.py`
- `app/migrations.py`
- `app/models.py`
- `app/workers/main.py`
- `web/src/App.vue`（去掉发放入口 + 资源池清理/灰显；工作区同时含 t952 设备中心默认页改动，提交时保留未回滚）
- `tests/test_comres_dedupe.py`（新）
- `tests/test_comres_prune.py`（新）
- `tests/test_comres_bound.py`（新，F6）
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md`

## c. 版本/提交证据

（提交后回填）

```text
$ git log -1 --oneline
（见提交后）

$ git status --short
（见提交后）

$ git diff --stat HEAD~1
（见提交后）
```

分支：`feat/comres`  
Gerrit change：不适用

## d. 验证/自测

- F6：`auto:sn` → `state=bound` / `allocated=false` / `bound=true`；真实租用 → `allocated=true`；同次 `list_resources` 并存（`tests/test_comres_bound.py`）
- F5：holder offline/retired/last_checked 过期 → takeover；连续注册 takeover 审计仅 1 条（`tests/test_comres_dedupe.py`）
- 去重/prune/apply 回归：一并 22 passed
- 真库 prune：本任务未对真库执行 prune
