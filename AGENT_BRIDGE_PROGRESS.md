# t1139 — 进度

> 任务：普通用户（role=user）可添加设备；机型/设备类型增删改仍仅 admin
> task_id: t1139
> 工作区：`/home/scm/github_gitee/quamm-res`
> 分支：`feat/capabilities`（HEAD 62cce60 v0.53.0，工作区大量未提交）

## 状态：已完成（停住等审）

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| 后端依赖 | probe/register/discover 由 `require_admin` 改 `require_login`，docstring 补 v0.54.5 | ✅ |
| 未放开 | 截图、probe-batch、删除设备等 admin 接口均未改 | ✅ |
| 前端按钮 | 概况标题、el-empty、品类详标题、细网格空态四处去除 admin 限制 | ✅ |
| 机型页 | 「新增机型」按钮保持 `v-if="me.role === 'admin'"` | ✅ |
| 新测试 | `tests/test_device_add_user.py` 5 例全绿 | ✅ |
| 版本 | VERSION 0.54.5，CHANGELOG `[0.54.5]` | ✅ |
| 构建 | `cd web && npm run build` 退出码 0，新 dist `index-5pGRhd4R.js` | ✅ |
| 禁止项 | 未 commit/push、未重启服务、未改其它仓/文档 | ✅ |

## 备注

- 进入任务时 `VERSION` 实际为 `0.54.6`（未提交，CHANGELOG 最高仍 0.54.4）。按任务明确目标写为 `0.54.5`，需确认是否与并行任务冲突。
- MCP：`res_device_probe`/`res_device_register` 本就无 admin 判定（仅 require_auth），与 Web 新语义一致；机型 MCP 工具仍 admin-only。
- 既有 `tests/test_registration.py` 2 例失败为历史遗留（seed 机型列表为空），与本任务无关。
