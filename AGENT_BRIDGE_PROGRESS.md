# t1058 flash image sources — 进度

> 任务：res_flash 增加 local / build_server 镜像来源 + res_flash_report  
> task_id: t1058  
> 工作区：`/home/scm/worktree/tres-flash-sources`  
> 分支：`feat/flash-image-sources`（基线 0.53.2 / 44fc7cf）

## 状态：已完成（停住等审）

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| FlashJob 列 | executor / image_meta / runner_id / progress / reported_at | ✅ |
| 迁移 | 幂等 ALTER flash_job（inspect 判表） | ✅ |
| 配置 | flash_agent_timeout_seconds=1800 + agent watchdog | ✅ |
| source=db | 兼容 + mode/executor 字段 | ✅ |
| source=local | WAITING_AGENT + command_argv + report_hint | ✅ |
| res_flash_report | RUNNING/SUCCEEDED/FAILED；幂等/冲突/FORBIDDEN | ✅ |
| source=build_server | 预检 + SSH 执行；无 secret 回显 | ✅ |
| MCP | res_flash 扩参；新增 res_flash_report | ✅ |
| 测试 | test_flash.py 24 passed（含 fixture 补机型） | ✅ |
| VERSION | 0.54.0 + CHANGELOG | ✅ |
| 提交 | feat(flash): support local/build-server image sources (0.54.0) | ✅ |
| 部署/push | 未部署 / 未 push | ✅ |

## 已知限制

- BuildServer 仍一账号一台（未改 principal_id unique）
- 193 未装 xosadb2update → RUNNER_TOOL_MISSING（不自动安装）
- source=local 不校验 Agent 侧路径是否存在
