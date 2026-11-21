---
task_id: t960
---

# t960：统计卡浅色配色 + 宽屏七列一排

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`built in 4.58s`；产物 `web/dist/index.html`、`web/dist/assets/index-CUykPCCz.js`、`web/dist/assets/index-14VofpKn.css`

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

1d44235 / 1d442356983cf9a593959763404fb51d4a0678d8  ← HEAD

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 前端构建 | `cd web && npm run build` | exit 0；CSS 含 `min-width: 1400px/1100px/800px`、`#fbfcfe`、`#eff6ff`、`#16a34a` |
| 断点 | ≥1400 七列等分(min 150px) / ≥1100 四列 / ≥800 三列 / &lt;800 两列 | 已写入 CSS |
| 浅色卡 | 白底边框圆角轻阴影；状态色数字；筛选蓝边+淡蓝底 | 已替换深色块 |
| 单测 | `pytest tests/test_device_stats.py -q` | 1 passed |
