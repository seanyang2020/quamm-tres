---
task_id: inner-4405
---

# inner-4405 验收证据（TRES 租约配额门禁：admin 豁免）

## a. 编译/构建

- 命令：`.venv/bin/python -m py_compile app/domain/leasing.py app/seed.py tests/test_quota.py`
- 退出码：`0`
- 关键结果：三个改动文件语法编译通过，无输出。

## b. 改动文件清单

- `app/domain/leasing.py` — `apply()` 配额门禁改为 `if not auth.is_admin:` 才检查 `count_active_leases` / `principal.quota`；admin 直接进入 ACL 与分配流程，普通用户行为不变。
- `app/seed.py` — `seed_admin()` 的 `quota=0` 语义保留，仅修正注释：说明 admin 不占配额、租约入口对 `is_admin` 豁免门禁。
- `tests/test_quota.py` — 新增 `test_admin_can_lease_with_zero_quota`：admin（quota=0）连续租 2 台均 GRANTED，`active_leases` 正常计数且 quota 仍为 0；原有 `test_quota_exceeded` 继续覆盖普通用户超额被 `QUOTA_EXCEEDED` 拒绝。
- `AGENT_BRIDGE_EVIDENCE.md`、`AGENT_BRIDGE_PROGRESS.md` — 本任务证据/进度。

未改：数据库既有记录、web/前端、其他模块、未跟踪的 evidence/powerres 文件。

## c. 版本/提交证据

- `git log -1 --oneline`：`e927470 fix(tres): 重启测试运行中 elapsed_s 实时刷新`
- `git status --short`（本任务相关）：
  - `M AGENT_BRIDGE_EVIDENCE.md`
  - `M AGENT_BRIDGE_PROGRESS.md`
  - `M app/domain/leasing.py`
  - `M app/seed.py`
  - `M tests/test_quota.py`
  - 其余为任务前既有的未跟踪文件（powerres.py、docs、db.bak、历史测试等），未触碰。
- `git diff --stat`：`app/domain/leasing.py | 17 +++++---`、`app/seed.py | 4 +++-`、`tests/test_quota.py | 33 ++++++++++`（另有证据文件自身 diff）
- 未 push、未提交、未合并；VERSION 保持 `0.54.12`。
- Gerrit change：无/不适用。

## d. 验证/自测

1. 定向 pytest（门禁相关全部通过）
   - 命令：`timeout 180 .venv/bin/python -m pytest tests/test_quota.py tests/test_device_acl.py tests/test_credentials_role.py -q`
   - 退出码：`0` → `16 passed, 7 warnings`
2. `tests/test_apply_flow.py`
   - 命令：`timeout 200 .venv/bin/python -m pytest tests/test_apply_flow.py -q`
   - 退出码：`1` → `2 passed, 2 failed`
   - 通过：`test_apply_renew_release`、`test_whoami_admin_flag`
   - 失败：`test_list_products_and_whoami`、`test_product_metadata_completeness_and_stats`，均为既有历史失败：`app/seed.py` 自 v0.17 起 `PRODUCT_TYPES = []`（有意移除 mock 机型预置），用例仍断言 5 个预置机型，与本门禁改动无关。
3. 合并跑（四文件）：`timeout 300 .venv/bin/python -m pytest tests/test_quota.py tests/test_apply_flow.py tests/test_credentials_role.py tests/test_device_acl.py -q` → 退出码 `1`，`18 passed, 2 failed`（失败同上，非本任务引入）。
4. 服务回归（本机 10.25:9100）
   - `systemctl --user restart quamm-tres.service quamm-worker.service` → 退出码 `0`，两服务 `active`
   - `curl http://127.0.0.1:9100/api/health` → HTTP 200，`{"status":"ok","version":"0.54.12",...}`
5. admin 真机实测（MCP `POST /mcp/`，`Authorization: Bearer <~/.config/quamm-tres/token>`）
   - `res_whoami` → `email=admin@corp.com, is_admin=true, quota=0, active_leases=0`
   - `res_apply(device_id=dev_080989bb98, agent_info=CodeBuddy/1.0-inner-4405-check, purpose=inner-4405 admin 配额门禁验证, expected_minutes=5)` → `state=GRANTED`，`lease_id=L-f39793a0a2`，`adb_address=172.16.60.249:5555`，`expires_at=2026-11-27T16:04:36`
   - `res_release(lease_id=L-f39793a0a2, reason=inner-4405 验证结束释放)` → `state=RELEASED, ok=true`
   - 复核 `res_whoami` → `active_leases=0`；`res_device_info(dev_080989bb98)` → `status=available, holder_email=null, locked=false`，无遗留占用。

## 201 生产部署方式（本次未执行）

本机无 201 SSH 通道，未部署。交付后在能访问 201 的跳板执行：

1. 本机导出离线 bundle（含本修复提交后）：
   `git bundle create ~/quamm-tres.bundle main`
2. 将 bundle 传到 201（scp/内网文件通道），在 201 仓库根执行：
   `bash scripts/deploy-git.sh /path/to/quamm-tres.bundle`
   （脚本会 `git fetch bundle` → `checkout -f -B main bundle/main` → 重启 `quamm-tres.service`/`quamm-worker.service` → 打印 health 与 VERSION）
3. 若 web 产物有更新需另行同步/构建；本任务未改前端。
