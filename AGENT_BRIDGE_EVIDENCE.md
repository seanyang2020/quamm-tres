---
task_id: t960
---

# t960：恢复设备中心顶部分类统计卡片

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`built in 4.72s`；产物 `web/dist/index.html`、`web/dist/assets/index-BexUe48V.js`、`web/dist/assets/index-Drb26jFR.css`（dist 被 gitignore，不入库）

- 命令：`.venv/bin/python -m pytest tests/test_device_stats.py -q --tb=short`
- 退出码：`0`
- 关键结果：`1 passed`

## b. 改动文件清单

- `web/src/App.vue`
- `tests/test_device_stats.py`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t960.md`
- `.agent-bridge/evidence/t960_PROGRESS.md`

## c. 版本/提交证据

```
80e65bf feat(web): 设备中心恢复分类统计卡片并可点击筛选
  80e65bf5c486f312828941c926dfcc15d9dad687
64f4fef docs(evidence): 回填 t960 统计卡片恢复验收证据
  49ad93e / 49ad93edc65395afd27e7fe0e0992798ed4b115c

git status --short  # 证据文件已同步
git diff --stat 80e65bf^..80e65bf
  web/src/App.vue / tests/test_device_stats.py / AGENT_BRIDGE_*.md / .agent-bridge/evidence/t960*
```

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0；bundle 含设备总量/已分配/串口资源/device-stats-row |
| 无看板菜单 | 源码无 `label: '看板'` | 无匹配 |
| 统计聚合单测 | `pytest tests/test_device_stats.py -q` | 1 passed（总量8/在线3/离线2/待检3/已分配1/空闲2） |
| 状态条 | 仅保留巡检时间；在线/离线/待检由统计卡展示 | 不重复 |
