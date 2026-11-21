---
task_id: t2-comres-tab
---

# T2 TRES 前端：COMRes Tab（合并 WireLog）+ 串口灰显 + 浏览器 TCP shell

## Diff 关键片段

### Tab 定义

```js
{ key: 'wirelog', label: 'COMRes' },
```

### 灰显逻辑

```js
function portHasSerial(row) {
  return !!(row.endpoint || row.com || row.com_name || row.tty
    || row.com_key || row.shell_reachable || row.tcp_port)
}
function portCanOpen(row) {
  if (!portHasSerial(row)) return false
  // shell_reachable / endpoint+slot / health online|ready
}
function wlNodeRowClass({ row }) {
  return wlNodeHasSerial(row.node_id) ? '' : 'comres-no-serial'
}
```

CSS：`.comres-muted` / `.comres-no-serial`；无串口行文案「无串口资源」，不渲染可点「打开串口」。

### 打开按钮

```js
window.open('/shell.html?' + q.toString(), '_blank')
// query: node_id / slot / name / tty / baud
```

模板：`v-if="portCanOpen(row)"` →「打开串口」；否则灰显「无串口资源」。

## 字段核对（与后端一致）

| 源 | 字段 |
|---|---|
| `GET /api/wirelog/nodes` | `node_id` / `ip` / `status`；在线端口数由 `/api/comres/resources` 按 `node_id` + `health_state` 聚合 |
| `GET /api/comres/resources` | `{ allocated, bound, pool }`；项含 `com` / `slot` / `endpoint` / `health_state` / `allocated` / `allocated_by` / `allocated_expire` / `bound_device_id` / `device_id` / `state` |
| sn / mac | resources 响应无此键 → UI 显示 `—`（不造数据）；节点详情可从 `bound_device.sn/mac` 或 `shell_mac` 回填 |

## 额外改动理由

| 文件 | 理由 |
|---|---|
| `web/public/shell.html` | 文案 WireLog→COMRes，与 tab 改名一致；缺 `node_id` 提示改为「打开串口」 |
| `web/src/main.js` | **未改**（无需） |

未改后端 Python；未新增前端依赖。

## a. 编译/构建

命令：`cd web && npm run build`  
退出码：`0`  
产物：`web/dist/index.html`、`web/dist/shell.html`、`web/dist/assets/index-*.js`（`dist/` 在 `.gitignore`）

构建摘要：

```text
vite v5.4.21 building for production...
✓ 1591 modules transformed.
dist/index.html                     0.40 kB │ gzip:   0.29 kB
dist/assets/index-CjQRsnL0.css    366.32 kB │ gzip:  49.31 kB
dist/assets/index-D0klTXWQ.js   1,166.93 kB │ gzip: 373.50 kB
✓ built in 3.60s
BUILD_EXIT:0
```

## b. 改动文件清单

- `web/src/App.vue`
- `web/public/shell.html`
- `AGENT_BRIDGE_PROGRESS.md`
- `AGENT_BRIDGE_EVIDENCE.md`
- `.agent-bridge/evidence/t2-comres-tab.md`

## c. 版本/提交证据

```text
5a3c1fd feat(comres): 串口上报 sn/mac 自动建档，资源视图三态
```

工作区（未 commit）：

```text
 M web/public/shell.html
 M web/src/App.vue
 # + evidence/progress
git diff --stat -- web/:
 web/public/shell.html |   6 +-
 web/src/App.vue       | 362 ++++++++++++++++++++++++++++++++++++++++++++------
```

Gerrit：不适用

## d. 验证/自测

- `npm run build` → exit 0；产物存在
- 字段名脚本核对：FE 含 `/api/comres/resources`、`allocated`/`bound`/`pool`、`allocated_expire`/`health_state`/`bound_device_id`/`allocated_by` → True
- dist JS 命中 `打开串口` / `COMRes` / `/api/comres/resources`
