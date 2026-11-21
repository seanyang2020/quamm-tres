---
task_id: tres-comres-apply
---

## a. 编译/构建

命令：`cd web && npm run build`  
退出码：`0`  
结果：`✓ built in 4.71s`；产物 `web/dist/index.html`、`web/dist/assets/index-BNmCIIOe.js` 存在（`dist/` 被 `.gitignore`，按习惯不入库）。

命令：`PYTHONPATH=. .venv/bin/python -m compileall -q app/domain/comres_apply.py app/api/comres_router.py app/domain/comres.py app/models.py app/migrations.py`  
退出码：`0`

命令：`PYTHONPATH=. .venv/bin/python -m pytest tests/test_comres_apply.py -v`  
退出码：`0`  
结果：`7 passed`

## b. 改动文件清单

- `app/models.py`（`WireLogClientApply` / `wl_client_apply`）
- `app/migrations.py`（`_ensure_wl_client_apply_table`）
- `app/domain/comres_apply.py`（新建：申请/审批/设置/限流）
- `app/api/comres_router.py`（apply / applications / apply-settings）
- `app/domain/comres.py`（资源池过滤未授权）
- `app/domain/wirelog.py`（节点 brief 带 `auth_status`）
- `web/src/App.vue`（授权抽屉、徽标、汇总）
- `tests/test_comres_apply.py`（新建）
- `requirements.txt`（`cryptography>=42.0`）
- `AGENT_BRIDGE_EVIDENCE.md`
- `AGENT_BRIDGE_PROGRESS.md`
- `.agent-bridge/evidence/tres-comres-apply.md`

## c. 版本/提交证据

（提交后回填）

```text
$ git log -1 --oneline
（待提交）

$ git status --short
（待提交）

$ git diff --stat
（见提交）
```

Gerrit change：不适用

## d. 验证/自测

命令：`pytest tests/test_comres_apply.py -v` → `7 passed`（exit 0）

三条路径演示（TestClient，等价 curl）：

1. **自动批准**：`POST /api/comres/apply` → `status=approved` + token；再次 apply → 同一 `node_id` / 同一 token（字节一致）
2. **待审→批准**：`auto_approve=false` → `pending`；管理面 `approve` 后 `GET /api/comres/apply/{client_id}` 回同一 token
3. **离线重取**：`GET /api/comres/apply/{client_id}` 与再次 `POST /apply` 均返回同一 token

管理面未登录：`GET /api/comres/applications` → `401`

前端：`npm run build` 通过；COMRes Tab 右上角「授权」打开抽屉（自动批准开关 + 待审列表批准/拒绝 + 已授权撤销）。
