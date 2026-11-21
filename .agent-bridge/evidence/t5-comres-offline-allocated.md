---
task_id: t5-comres-offline-allocated
---

# T5 COMRes：offline 仅对未分配端口

## 问题

真机：PC deskserialhub 对已分配端口停本地心跳；T4「未上报即 offline」把 COM16/COM21/COM4 误标离线。

## 修复

`_mark_absent_ports_offline`：

- `_is_allocated` 为真 → 保持 `health_state`，审计 `comres_port_skipped_allocated`
- 未分配 → `offline` + `comres_port_offline`（T4 不变）

## a. 编译/构建

`PYTHONPATH=. .venv/bin/python -m compileall -q app/domain/comres.py tests/smoke_comres.py` → exit 0

## b. 改动文件

- `app/domain/comres.py`
- `tests/smoke_comres.py`
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md`
- 本文件

## c. 版本/提交

```text
$ git log -1 --oneline
fa04ffb fix(comres): 已分配端口未上报不置 offline，仅未分配走 T4 下线

$ git diff --stat f71d137..fa04ffb
 app/domain/comres.py     | 19 ++++-
 tests/smoke_comres.py    | 90 +++++++++++++++++++++-
 (+ evidence / PROGRESS)
```

## d. 验证

`PYTHONPATH=. .venv/bin/python tests/smoke_comres.py` → `RESULT ALL_PASS` / exit 0
