---
task_id: t2-comres-tab
---

## a. 编译/构建

命令：`cd web && npm run build`  
退出码：`0`  
结果：`web/dist/index.html`、`web/dist/shell.html`、`web/dist/assets/index-D0klTXWQ.js` 已生成；无 TS/编译报错。

```text
✓ 1591 modules transformed.
✓ built in 3.60s
BUILD_EXIT:0
```

## b. 改动文件清单

- `web/src/App.vue` — Tab 改名 COMRes；节点视图 + `/api/comres/resources` 资源池；灰显；打开串口；401/失败 el-alert
- `web/public/shell.html` — 文案 WireLog→COMRes
- `.agent-bridge/evidence/t2-comres-tab.md` — 本任务证据
- `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md` — 进度与验收四要素

未改：`web/src/main.js`、后端 Python、其他 tab。

## c. 版本/提交证据

```text
$ git log -1 --oneline
5a3c1fd feat(comres): 串口上报 sn/mac 自动建档，资源视图三态

$ git status --short
 M AGENT_BRIDGE_EVIDENCE.md
 M AGENT_BRIDGE_PROGRESS.md
 M web/public/shell.html
 M web/src/App.vue
?? .agent-bridge/evidence/t2-comres-tab.md
# （另有他任务遗留 untracked evidence，与本任务无关）

$ git diff --stat -- web/src/App.vue web/public/shell.html
 web/public/shell.html |   6 +-
 web/src/App.vue       | 362 ++++++++++++++++++++++++++++++++++++++++++++------
```

Gerrit：不适用  
本任务前端改动尚未 commit。

## d. 验证/自测

- `cd web && npm run build` → exit 0；dist 产物存在（`dist/` gitignore）
- 字段对齐：`list_resources` → `allocated`/`bound`/`pool`；项字段 `com`/`slot`/`endpoint`/`health_state`/`allocated`/`allocated_by`/`allocated_expire`/`bound_device_id`；FE 引用一致；`sn`/`mac` 后端 resources 无则 UI 留空
- dist 产物字符串命中 `COMRes` / `打开串口` / `/api/comres/resources`
