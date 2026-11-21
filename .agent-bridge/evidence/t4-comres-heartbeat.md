---
task_id: t4-comres-heartbeat
---

# T4 TRES COMRes：全量心跳下线语义 + 自动绑定保活周期

## 契约选择（A）

`/api/comres/resources` **在线视图剔除** `health_state=offline` 的端口（不进入 `allocated` / `bound` / `pool`）。

- 绑定与 `bound_device_id` 保留在库内；再次上报恢复 `online` 后重新出现在三态中。
- 不采用 `state="offline"` 回传，避免与既有三态 `state` 枚举混用。
- 审计：`comres_port_offline`（detail：`com` / `com_key` / `endpoint` / `was_health_state`）。
- `unregister` 仍精确删除指定 com+endpoint，行为未改。

## Diff 摘要

| 文件 | 变更 |
|---|---|
| `app/domain/comres.py` | 全量心跳下线；`AUTO_ALLOCATED_TTL_S=7200`；auto 续保活；节点 stale 释放 auto；resources 剔除 offline |
| `tests/smoke_comres.py` | ACCEPT-9：下线/恢复/auto TTL/手工 15min/stale |

未改：`app/api/comres_router.py`（域层足够）、`/api/wirelog`、deskserialhub。

提交：`f71d137`（父：`5d739a7`）

## a. 编译/构建

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/domain/comres.py tests/smoke_comres.py`  
退出码：`0`

## b. 改动文件清单

- `app/domain/comres.py`
- `tests/smoke_comres.py`
- `.agent-bridge/evidence/t4-comres-heartbeat.md`
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md`

## c. 版本/提交证据

```text
f71d137 feat(comres): 全量心跳下线未上报端口，auto 占用保活≥2×心跳
```

`git diff --stat 5d739a7..f71d137`：2 files, +294/-25

## d. 验证/自测

### smoke

```text
$ PYTHONPATH=. .venv/bin/python tests/smoke_comres.py
...
=== ACCEPT-9 heartbeat offline + auto TTL ===
PASS 未报端口变 offline
PASS offline 不在在线视图
PASS 再上报恢复 online
PASS auto 占用 TTL≥7200
PASS auto 3601s 后仍已分配
PASS auto 7201s 后释放占用
PASS 手工占用缺省 TTL=15min
PASS 手工占用 15min 后释放
PASS 节点 stale 释放 auto 占用
RESULT ALL_PASS
SMOKE_EXIT:0
```

### 本地对接（原两条 FAIL → PASS）

```text
$ timeout 60 .venv/bin/python /home/scm/tmp/comres_local_verify.py
PASS 心跳后 COM-V2 退出在线视图  n=1
PASS 解注册后该节点无在线端口  n=0
RESULT ALL_PASS
VERIFY_EXIT:0
```

## 实现要点

- **全量语义**：`register` / 带非空 `ports[]` 的 `heartbeat` 结束后，对同 node 未出现的既有绑定置 `offline`；空 `ports` 心跳仅刷新节点存活，不下线。
- **auto 保活**：`_mark_allocated(by="auto:…")` 缺省 TTL=`2*HEARTBEAT_INTERVAL_S`（7200）；再上报续期；节点 `last_heartbeat_at` 超 7200s → `comres_auto_release_stale`。
- **手工占用**：`allocated_by` 非 `auto:` 前缀仍用 `ALLOCATED_TTL_S=900`。
