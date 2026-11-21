---
task_id: t960
---

# t960：设备中心统计卡布局（断点 / 末行居中 / 等高高亮）

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`built in 4.18s`；产物 `web/dist/index.html`、`web/dist/assets/index-B4Di2LWU.js`、`web/dist/assets/index-C-rRhTjo.css`（dist 被 gitignore）

- 命令：`.venv/bin/python -m pytest tests/test_device_stats.py -q --tb=short`
- 退出码：`0`
- 关键结果：`1 passed`

## b. 改动文件清单

- `web/src/App.vue`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t960.md`
- `.agent-bridge/evidence/t960_PROGRESS.md`

## c. 版本/提交证据

3a187dd / 3a187ddd06614952b8387e70dda950f94ea351c2  ← HEAD

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0；CSS 含 `device-stats-grid` / `min-width: 900px` / `min-width: 1200px` |
| 断点 | ≥1200→4列 / ≥900→3列 / &lt;700→2列；`justify-content: center` | 7 卡末行居中（4+3 / 3+3+1）不孤左 |
| 串口卡 | 固定第 7 张普通卡位，副标题「在池 N」 | 始终占位 |
| 筛选高亮 | `.ov-card.is-active` 描边 | 2px 蓝边 + 外发光 |
| 单测 | `pytest tests/test_device_stats.py -q` | 1 passed |
