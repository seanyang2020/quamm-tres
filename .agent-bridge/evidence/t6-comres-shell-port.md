---
task_id: t6-comres-shell-port
---

## a. 编译/构建

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/api/wirelog_router.py tests/test_wirelog_shell_target.py` → exit `0`  
命令：`PYTHONPATH=. .venv/bin/python -m pytest tests/test_wirelog_shell_target.py -q` → exit `0`，`5 passed`

## b. 改动文件清单

- `app/api/wirelog_router.py`
- `tests/test_wirelog_shell_target.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t6-comres-shell-port.md`

## c. 版本/提交证据

（提交后由主证据文件回填；见仓库 `git log -1`）

Gerrit：不适用

## d. 验证/自测

`_resolve_shell_target` 对 wle19b7266e729846c 形态（endpoint=`tcp://172.16.10.25:9210`）解析结果为 `tcp://172.16.10.25:9210`（exit 0）。
