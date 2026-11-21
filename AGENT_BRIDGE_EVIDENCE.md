---
task_id: t960
---

# t960：恢复设备中心顶部分类统计卡片

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`✓ built in 4.72s`；产物 `web/dist/index.html`、`web/dist/assets/index-BexUe48V.js`、`web/dist/assets/index-Drb26jFR.css`（`dist/` 被 `.gitignore`，不入库）

- 命令：`.venv/bin/python -m pytest tests/test_device_stats.py -q --tb=short`
- 退出码：`0`
- 关键结果：`1 passed`

## b. 改动文件清单

- `web/src/App.vue`
- `tests/test_device_stats.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`

## c. 版本/提交证据

```
1369ee6 feat(web): 设备中心恢复分类统计卡片并可点击筛选
%H: 1369ee6a851966a789e3d4aeccf7275b5f6195d5

git status --short  # clean
git diff --stat HEAD~1
 .agent-bridge/evidence/t960.md          |  48 +++++++++++
 .agent-bridge/evidence/t960_PROGRESS.md |  25 ++++++
 AGENT_BRIDGE_EVIDENCE.md                |  48 ++++++-----
 AGENT_BRIDGE_PROGRESS.md                |  18 ++---
 tests/test_device_stats.py              | 103 ++++++++++++++++++++++++
 web/src/App.vue                         | 138 ++++++++++++++++++++++++++++++--
 6 files changed, 343 insertions(+), 37 deletions(-)
```

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0；bundle 含 `设备总量`/`已分配`/`串口资源`/`device-stats-row` |
| 无看板菜单 | `rg "label: '看板'" web/src/App.vue` | 无匹配 |
| 统计聚合单测 | `pytest tests/test_device_stats.py -q` | `1 passed`（总量 8 / 在线 3 / 离线 2 / 待检 3 / 已分配 1 / 空闲 2） |
| 状态条 | 模板仅保留巡检时间，在线/离线/待检改由统计卡展示 | 不重复 |
