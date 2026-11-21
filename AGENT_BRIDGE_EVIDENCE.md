---
task_id: t969
---

# t969：TRES 侧可达性探测（reach）与自检分口径

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`built in 4.41s`；产物 `web/dist/index.html`、`web/dist/assets/index-BFzzaYKL.js`、`web/dist/assets/index-14VofpKn.css`

- 命令：`.venv/bin/python -m pytest tests/test_comres_reach.py -q --tb=short`
- 退出码：`0`
- 关键结果：`10 passed`

## b. 改动文件清单

- `app/models.py`
- `app/migrations.py`
- `app/config.py`
- `app/domain/comres.py`
- `app/api/comres_router.py`
- `app/api/wirelog_router.py`
- `app/workers/main.py`
- `web/src/App.vue`
- `tests/test_comres_reach.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t969.md`
- `.agent-bridge/evidence/t969_PROGRESS.md`

## c. 版本/提交证据

575a461 / 575a461762de5c4c589d48267b647d0f5555527b  ← 可达性改动
dc41cdf / dc41cdf33430fc6200916684b058f73edacde79d  ← HEAD（证据回填）

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 单测 | `pytest tests/test_comres_reach.py -q` | 10 passed；exit 0 |
| 前端构建 | `cd web && npm run build` | exit 0 |
| 真机可达 | `POST /api/comres/reachability` | `checked=6 ok=5`；`wl8e9a154c0e5b35f3/ttyUSB0` → `reach_ok=false, reach_error=no_route`；COM16/21/4/27/24 → ok |
| 打开串口 | WS `/api/wirelog/nodes/wl8e9a154c0e5b35f3/ports/1/shell` | 立刻 `{"type":"error","code":"UNREACHABLE","message":"…（no_route）：tcp://172.16.60.122:9001…"}` |
