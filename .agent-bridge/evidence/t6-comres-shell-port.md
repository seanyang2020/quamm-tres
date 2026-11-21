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

```text
$ git log -1 --oneline
61057ae fix(comres): shell 按 binding.endpoint 解析端口，避免 caps.raw_tcp_port 连错 PC 串口

$ git status --short
?? .agent-bridge/evidence/t4-comres-heartbeat.md
（及其余历史 evidence 未跟踪文件）

$ git diff --stat HEAD~1
 app/api/wirelog_router.py + tests/test_wirelog_shell_target.py + 证据文件
 5 files changed, 184 insertions(+), 41 deletions(-)
```

Gerrit：不适用

## d. 验证/自测

`_resolve_shell_target` 对 wle19b7266e729846c 形态（endpoint=`tcp://172.16.10.25:9210`）解析结果为 `tcp://172.16.10.25:9210`（exit 0）。
