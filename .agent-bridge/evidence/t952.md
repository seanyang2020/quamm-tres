---
task_id: t952
---

# t950 / t952：TRES 信息架构调整（设备中心默认页）

## a. 编译/构建

命令：`cd web && npm run build`  
退出码：`0`  
结果：`✓ built in 5.00s`；产物 `web/dist/index.html`、`web/dist/assets/index-fJvM_eBC.js` 存在（`dist/` 被 `.gitignore`，与既有做法一致不入库）。

命令：`PYTHONPATH=. .venv/bin/python -m pytest tests -q`  
退出码：`1`  
结果：`135 passed, 21 failed`。失败集与 t948 基线同批（apply_flow / flash / mcp_http / prober_l3 / queue / registration），**本任务仅前端 IA，未新增失败**。

## b. 改动文件清单

- `web/src/App.vue`（去看板；默认「设备中心」；状态条 + 机型 chips；最近探测列/排序；编辑弹层上次探测；旧 `#/board` 重定向；设置页无运行时状态块可删）
- `web/dist/*`（重建，不入库）
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md`
- 截图：`.agent-bridge/evidence/t950-shots/{devices,devices-list,settings,home}.png`

说明：主体 IA 改动曾与并行任务 t948 同文件工作区合并进 `d91cb8d`；本提交补 hashchange 重定向与证据落盘。

## c. 版本/提交证据

```
a43fb7f feat(web): 设备中心默认页与信息架构调整
aa64e27 docs(evidence): 回填 t948 提交哈希与验收四要素
d91cb8d feat(comres): 资源池去重 + auto 绑定口径 + 端点接管 + 移除发放入口

git status --short  # 仅他任务遗留 untracked evidence / db.bak
```

Gerrit change：不适用。

关联：`d91cb8d`（t948 commit 曾合并本任务 App.vue IA 主体）；本任务跟进 `a43fb7f`。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 默认落地 | 登录后打开 `http://172.16.10.25:9100/` | 菜单首项「设备中心」激活；无「看板」 |
| 状态条 | CDP 读 `.device-status-bar` | `上次全量巡检 … · 最近快轮询 … · 在线 8 · 离线 10 · 待检 7` +「立即巡检」 |
| 机型 chips | `.product-chip` | `esl2_hk 0/0,framex 3/0,…`；点击进入该机型明细 |
| 最近探测列 | DOM `innerText.includes("最近探测")` | `YES`（表宽需横向滚动可见） |
| 旧路由 | 打开 `/#/board` | active=`设备中心`（hash 清除） |
| 设置页 | 系统设置 tab | 仅参数表单；无「上次全量巡检」运行时块（`no-runtime`） |
| 线上 bundle | `curl` 静态资源 | 含 `设备中心`/`立即巡检`/`最近探测`；`看板` 仅出现在 legacy key 集合 |

截图：

- 设备中心概况：`.agent-bridge/evidence/t950-shots/devices.png`
- 设备列表明细：`.agent-bridge/evidence/t950-shots/devices-list.png`
- 设置页：`.agent-bridge/evidence/t950-shots/settings.png`
