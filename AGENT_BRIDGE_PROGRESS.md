# Quamm TRES 实现进度

> 任务：T2 TRES 前端 COMRes Tab（合并 WireLog）+ 串口灰显 + 浏览器 TCP shell  
> task_id: t2-comres-tab；承接后端 `30f80a3` / T3 `5a3c1fd` COMRes API

## 状态：已完成（未 commit，停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| Tab 改名 | `{ key: 'wirelog', label: 'COMRes' }` | ✅ |
| 节点视图 | `/api/wirelog/nodes`：id / ip / 状态 / 在线端口数；详情端口列扩展 | ✅ |
| 资源池视图 | `/api/comres/resources` → allocated + bound + pool 合并表 | ✅ |
| 灰显 | 无串口 → `.comres-no-serial` +「无串口资源」；无打开按钮 | ✅ |
| 打开串口 | `window.open('/shell.html?node_id&slot&tty&baud&name')` | ✅ |
| 占用标注 | allocated → 占用者 + allocated_expire；未分配 → 资源池 / 已绑定未分配 | ✅ |
| 错误提示 | nodes / resources / detail 失败与 401 → el-alert + ElMessage | ✅ |
| shell.html | 文案对齐 COMRes | ✅ |

## 验收

- `web/` 下 `npm run build` exit 0；dist 更新
- 字段名与 `/api/comres/resources`、`/api/wirelog/nodes` 一致；无 sn/mac 不造数据
- 证据：`AGENT_BRIDGE_EVIDENCE.md` + `.agent-bridge/evidence/t2-comres-tab.md`

## 额外改动理由

| 文件 | 理由 |
|---|---|
| `web/public/shell.html` | tab 改名后用户提示/缺参文案同步为 COMRes |

未改 `web/src/main.js`、后端 Python、其他 tab。

## 说明

- 未推远程、未合 main；前端未单独 commit；停住等审
