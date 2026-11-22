---
task_id: t981
---

# t981：TRES 设备指纹接收/存储/展示 + 匹配 MAC→SN→IP

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`✓ built in 3.89s`；产物 `web/dist/index.html`、`web/dist/assets/index-BpaA9ib2.js`、`web/dist/assets/index-DweI5MbN.css`

- 命令：`.venv/bin/python -m pytest tests/test_comres_fingerprint.py -q --tb=short`
- 退出码：`0`
- 关键结果：`9 passed`

- 命令：`.venv/bin/python -m pytest tests/test_comres_fingerprint.py tests/test_comres_bound.py tests/test_comres_reach.py -q --tb=no`
- 退出码：`0`
- 关键结果：`22 passed`

- 命令：`.venv/bin/python -m pytest tests/ -q --tb=no`
- 退出码：`1`（基线失败集）
- 关键结果：`181 passed, 21 failed`；失败集与基线同批（apply_flow / flash / mcp_http / prober_l3 / queue / registration）；本任务新增 9 passed

## b. 改动文件清单

- `app/models.py`
- `app/migrations.py`
- `app/domain/comres.py`
- `app/domain/wirelog.py`
- `app/api/comres_router.py`
- `web/src/App.vue`
- `tests/test_comres_fingerprint.py`
- `.agent-bridge/evidence/t981-shots/fp-drawer.png`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t981.md`
- `.agent-bridge/evidence/t981_PROGRESS.md`

## c. 版本/提交证据

```
0151105 / 0151105b3214078a0d3bc46afa9d8203f5af245d
feat(comres): 接收存储展示设备指纹，匹配顺序改为 MAC→SN→IP
git status --short → 仅残留无关 t979 evidence（未纳入本提交）
git diff --stat HEAD~1..HEAD → 8 files, +796/-23
```

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0 |
| 新单测 | `pytest tests/test_comres_fingerprint.py -q` | 9 passed；exit 0 |
| 相关回归 | fingerprint+bound+reach | 22 passed；exit 0 |
| 全量 pytest | `pytest tests/ -q` | 181 passed, 21 failed；exit 1 |
| 服务重启 | `bash scripts/restart.sh` | units active；`/api/health` ok |
| 真机 register | `POST /api/comres/register` 带 fingerprint | DB 落库 `fp.v1`/`ok`；`shell_mac`/`shell_ip` 回填 |
| resources | `GET /api/comres/resources` | `fp_verdict=ok`，`fingerprint.schema=fp.v1`，identity.mac 正确 |
| 单查 | `GET /api/comres/ports/{node}/{slot}/fingerprint` | `hostname=t981-dut`（冒烟）/`t981-ui`（截图） |
| 截图 | `.agent-bridge/evidence/t981-shots/fp-drawer.png` | 指纹抽屉：网卡/标识/系统/构建 |
| 清理 | 删除测试 node/port/device | 已清理 |
