---
task_id: t979
---

# t979：设备中心串口按钮灰显 + 去掉写授权面板

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`✓ built in 4.86s`；产物 `web/dist/index.html`、`web/dist/assets/index-B66oPbsL.js`、`web/dist/assets/index-D6OiA4EK.css`

- 命令：`.venv/bin/python -m pytest tests/test_serial_openable.py -q --tb=short`
- 退出码：`0`
- 关键结果：`8 passed`

- 命令：`.venv/bin/python -m pytest tests/ -q --tb=no`
- 退出码：`1`
- 关键结果：`172 passed, 21 failed`；失败集与基线同批（apply_flow / flash / mcp_http / prober_l3 / queue / registration）；本任务新增 8 passed，失败数仍为 21

## b. 改动文件清单

- `app/domain/catalog.py`
- `app/api/wirelog_router.py`
- `web/src/App.vue`
- `tests/test_serial_openable.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t979.md`
- `.agent-bridge/evidence/t979_PROGRESS.md`
- `.agent-bridge/evidence/t979-shots/device-serial-btns.png`

## c. 版本/提交证据

```
7834e4f / 7834e4f10546abbaf76b295b2b3c7d0bf9bd732b
git status --short → 干净（功能提交后）
git diff --stat HEAD~1..HEAD → 9 files, +370/-86
```

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0 |
| 新单测 | `pytest tests/test_serial_openable.py -q` | 8 passed；exit 0 |
| 全量 pytest | `pytest tests/ -q` | 172 passed, 21 failed；exit 1 |
| 服务重启 | `bash scripts/restart.sh` | units active；`/api/health` ok |
| API 分布 | `GET /api/devices?limit=500`（admin cookie） | 25 台；`serial_openable` True=4 / False=21；`serial_ports` hist {0:20, 1:5} |
| 真机按钮 | puppeteer 未分类表「串口」 | total=9, enabled=4, disabled=5；`is-disabled` + cursor `not-allowed` |
| 写授权 UI | COMRes 节点详情 | `HAS_WRITE_GRANT_UI=false`（无「授予写权限」/「写授权」标题） |
| 截图 | `.agent-bridge/evidence/t979-shots/device-serial-btns.png` | 设备中心操作列 |
