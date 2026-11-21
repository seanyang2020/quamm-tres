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

```
937f7f2 fix(comres): 统一节点授权口径并默认开放串口写权限
937f7f29b86e9c4509b0da9dd1d714f7434e44de
```

- `git status --short`：干净（证据哈希回填前可能有 docs 未提交，见后续 evidence commit）
- `git diff --stat HEAD~1..HEAD`：14 files, +672/-56
- Gerrit change：不适用

## d. 验证/自测

| 项 | 命令/操作 | 结果 |
|---|---|---|
| 回填 dry-run | `scripts/backfill_enroll_applies.py --dry-run` | candidates=1（esl2-122） |
| 回填执行 | `scripts/backfill_enroll_applies.py` | created；二次 dry-run=0 |
| 库备份 | `~/tmp/quamm_res_pre_t966_20261121_193112.db` | 已备份 |
| 服务重启 | `systemctl --user restart quamm-tres.service` | active；/api/health 200 |
| 真库 API | `GET http://127.0.0.1:9100/api/wirelog/nodes` | esl2-122: `approved`/`token`；DT28: `approved`/`apply` |
| 单测 | pytest 上述 | 14 passed exit 0 |
| 前端构建 | `npm run build` | exit 0 |
