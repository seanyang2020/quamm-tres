---
task_id: t1058
---

# t1058：flash 镜像来源 local / build_server（0.54.0）

## a. 编译/构建

- 命令：`timeout 60 /home/scm/github_gitee/quamm-res/.venv/bin/python -m py_compile app/domain/flash.py app/models.py app/migrations.py app/config.py app/mcp/server.py`
- 退出码：`0`
- 关键结果：语法编译通过（无独立前端构建）

## b. 改动文件清单

- `VERSION` — 0.53.2 → 0.54.0
- `CHANGELOG.md` — 0.54.0 条目
- `app/config.py` — `flash_agent_timeout_seconds`
- `app/models.py` — FlashJob 新列
- `app/migrations.py` — 幂等 ADD COLUMN flash_job
- `app/domain/flash.py` — local/build_server/report/watchdog
- `app/mcp/server.py` — res_flash 扩参 + res_flash_report
- `tests/test_flash.py` — 新用来源用例 + 机型夹具修复
- `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md` — 本任务证据

## c. 版本/提交证据

- 分支：`feat/flash-image-sources`
- `git log -1 --oneline`：（提交后填写）
- `git status --short`：（提交后应干净或仅证据文件）
- `git diff --stat`：（相对基线）
- Gerrit change：不适用
- 部署：未部署；未 push

## d. 验证/自测

### d1. 目标测试

```
timeout 120 /home/scm/github_gitee/quamm-res/.venv/bin/python -m pytest tests/test_flash.py -q --tb=no
```

- 退出码：`0`
- 结果：`24 passed`

### d2. 覆盖点

1. source=local script → WAITING_AGENT / command_argv / job 字段  
2. source=local partition 命令（无 -e data）  
3. res_flash_report RUNNING→SUCCEEDED；终态冲突 JOB_ALREADY_FINISHED  
4. 非持有人 report → FORBIDDEN  
5. build_server mock run_remote：预检/路径/命令/成功失败  
6. source=db mode=server_db 不回归  
