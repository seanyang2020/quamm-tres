# t1059 flash agent watchdog — 进度

> 任务：Agent 上报 RUNNING 后失联时，watchdog 标 FAILED 释放租约  
> task_id: t1059  
> 工作区：`/home/scm/worktree/tres-flash-watchdog`  
> 分支：`fix/flash-agent-watchdog`（基线 0.54.0 / 5b22c5c）

## 状态：已完成（停住等审）

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| watchdog | 周期检查（45s）；WAITING_AGENT/RUNNING + last_activity 超时 → FAILED | ✅ |
| 终态保护 | flash_report 已有 JOB_ALREADY_FINISHED，确认保留 | ✅ |
| 测试 | stale RUNNING → FAILED；SUCCEEDED 不改；26 flash passed | ✅ |
| VERSION | 0.54.1 + CHANGELOG | ✅ |
| 提交 | `2c684ec` fix(flash): watchdog stale agent running jobs (0.54.1) | ✅ |
| 部署/push | 未部署 / 未 push | ✅ |

## 已知限制

- BuildServer 仍一账号一台（未改）
- 未改 `res_flash` / `res_flash_report` 对外契约
