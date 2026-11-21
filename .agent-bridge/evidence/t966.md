---
task_id: t966
---

# t966：统一节点授权口径 + 串口写权限默认开放

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：`built in 3.93s`；产物 `web/dist/index.html`、`web/dist/assets/index-PE0-xzTC.js`、`web/dist/assets/index-C-rRhTjo.css`

- 命令：`.venv/bin/python -m pytest tests/test_auth_source_and_write.py tests/test_comres_apply.py -q --tb=line`
- 退出码：`0`
- 关键结果：`14 passed`

## b. 改动文件清单

- `app/models.py`
- `app/migrations.py`
- `app/domain/comres_apply.py`
- `app/domain/wirelog.py`
- `app/domain/comres.py`
- `app/api/wirelog_router.py`
- `scripts/backfill_enroll_applies.py`
- `tests/test_auth_source_and_write.py`
- `web/src/App.vue`
- `web/public/shell.html`
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/t966.md`
- `.agent-bridge/evidence/t966_PROGRESS.md`

## c. 版本/提交证据

（commit 后回填 `git log -1` / `git status --short` / `git diff --stat`）

Gerrit change：不适用。

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 回填 dry-run | `scripts/backfill_enroll_applies.py --dry-run` | candidates=1（esl2-122） |
| 回填执行 | `scripts/backfill_enroll_applies.py` | created enroll apply；二次 dry-run candidates=0 |
| 库备份 | `cp quamm_res.db ~/tmp/quamm_res_pre_t966_*.db` | 已备份 |
| 真库 API | `GET /api/wirelog/nodes`（9100，重启后） | esl2-122: `auth_status=approved, auth_source=token`；DT28: `approved/apply` |
| 单测 | pytest 上述文件 | 14 passed |
| 前端构建 | `npm run build` | exit 0 |
