---
task_id: t1059
---

# t1059：flash agent watchdog 覆盖 RUNNING 失联（0.54.1）

## a. 编译/构建

- 命令：`timeout 60s /home/scm/github_gitee/quamm-res/.venv/bin/python -m py_compile app/domain/flash.py`
- 退出码：`0`
- 关键结果：语法编译通过（无独立前端构建）

- 命令：`timeout 120s /home/scm/github_gitee/quamm-res/.venv/bin/python -m pytest tests/test_flash.py -q --tb=line`
- 退出码：`0`
- 关键结果：`26 passed in 21.30s`（原 24 + 新增 2）

## b. 改动文件清单

- `VERSION` — 0.54.0 → 0.54.1
- `CHANGELOG.md` — 0.54.1 条目
- `app/domain/flash.py` — `_agent_watchdog` 周期性 stale 检查；`_agent_last_activity`
- `tests/test_flash.py` — stale RUNNING → FAILED；SUCCEEDED 不改
- `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md` — 本任务证据

## c. 版本/提交证据

- `git log -1 --oneline`：`2c684ec fix(flash): watchdog stale agent running jobs (0.54.1)`
- `git status --short`：（功能提交后工作区仅证据文件待定稿）
- `git diff --stat HEAD~1..HEAD`：
  ```
   CHANGELOG.md        |  9 +++++++++
   VERSION             |  2 +-
   app/domain/flash.py | 66 ++++++++++++++++++++++++++++++----------
   tests/test_flash.py | 87 +++++++++++++++++++++++++++++++++++++++++++++++++++++
   4 files changed, 147 insertions(+), 17 deletions(-)
  ```
- Gerrit change：无/不适用
- 全量 hash：`2c684ece716098f81ae6b6d3b337170ef72ab630`
- 分支：`fix/flash-agent-watchdog`（基线 `5b22c5c` / 0.54.0）
- 未 push / 未部署

## d. 验证/自测

- `pytest tests/test_flash.py -q` → 退出码 0，26 passed
- 覆盖：`test_agent_watchdog_fails_stale_running`（RUNNING + 回拨 reported_at → FAILED，含「Agent 超过」；终态后再 report RUNNING → `JOB_ALREADY_FINISHED`）
- 覆盖：`test_agent_watchdog_leaves_succeeded_alone`
- `flash_report` 终态保护确认：`job.status in _TERMINAL_STATUSES` → 冲突抛 `JOB_ALREADY_FINISHED`
