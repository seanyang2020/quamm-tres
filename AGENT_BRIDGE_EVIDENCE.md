---
task_id: t6-comres-shell-port
---

## a. 编译/构建

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/api/wirelog_router.py tests/test_wirelog_shell_target.py`  
退出码：`0`  
结果：无语法错误。

命令：`PYTHONPATH=. .venv/bin/python -m pytest tests/test_wirelog_shell_target.py -q`  
退出码：`0`  
结果：`5 passed in 0.14s`

## b. 改动文件清单

- `app/api/wirelog_router.py`
- `tests/test_wirelog_shell_target.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t6-comres-shell-port.md`

未改：其它 domain / router / 前端模块。

## c. 版本/提交证据

```text
$ git log -1 --oneline
61057ae fix(comres): shell 按 binding.endpoint 解析端口，避免 caps.raw_tcp_port 连错 PC 串口

$ git status --short
?? .agent-bridge/evidence/t4-comres-heartbeat.md
?? .agent-bridge/evidence/t857.md
?? .agent-bridge/evidence/t870.md
?? .agent-bridge/evidence/t880.md
?? .agent-bridge/evidence/t902.md
?? .agent-bridge/evidence/t915.md

$ git diff --stat HEAD~1
 .agent-bridge/evidence/t6-comres-shell-port.md | 26 ++++++++++
 AGENT_BRIDGE_EVIDENCE.md                       | 58 ++++++++++++----------
 AGENT_BRIDGE_PROGRESS.md                       | 22 ++++----
 app/api/wirelog_router.py                      | 50 +++++++++++++++++--
 tests/test_wirelog_shell_target.py             | 69 ++++++++++++++++++++++++++
 5 files changed, 184 insertions(+), 41 deletions(-)
```

Gerrit change：不适用

## d. 验证/自测

命令：
```bash
PYTHONPATH=. .venv/bin/python -c "
from types import SimpleNamespace
from app.api.wirelog_router import _resolve_shell_target
node = SimpleNamespace(ip='172.16.10.25', caps_json=None, shell_ip=None)
binding = SimpleNamespace(endpoint='tcp://172.16.10.25:9210', tcp_port=9210, shell_ip=None, com_name='COM16')
host, port, ep = _resolve_shell_target(node, binding, 1)
print(ep)
assert ep == 'tcp://172.16.10.25:9210'
"
```
结果：打印 `tcp://172.16.10.25:9210`，断言通过，退出码 `0`。

pytest 覆盖：(a) endpoint 优先；(b) tcp_port+ip；(c) caps 老节点回退；另含 binding.shell_ip 与无 binding。
