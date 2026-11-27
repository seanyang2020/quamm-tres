---
task_id: t1139
---

# t1139 验收证据（普通用户可添加设备，机型仍 admin）

## a. 编译/构建

- 命令：`cd web && npm run build`
- 退出码：`0`
- 关键结果：构建成功，产物存在
  - `web/dist/assets/index-5pGRhd4R.js`（1,282.07 kB）
  - `web/dist/assets/index-BzwBFL8y.css`（376.00 kB）
  - `web/dist/index.html`

## b. 改动文件清单

- `app/api/router.py` — `web_device_probe` / `web_device_register` / `web_device_discover` 依赖改 `require_login`，docstring 补 v0.54.5 说明；其它 admin 接口未改
- `web/src/App.vue` — 四处「＋ 添加设备」去除 `me.role === 'admin'` 限制
- `tests/test_device_add_user.py` — 新增 5 例（user probe/register/discover、user product-types 403、admin 四接口回归）
- `VERSION` — 0.54.6 → 0.54.5（按任务目标；工作区此前已被其它任务改为 0.54.6，见备注）
- `CHANGELOG.md` — 顶部新增 `[0.54.5]` 条目
- `AGENT_BRIDGE_EVIDENCE.md` / `AGENT_BRIDGE_PROGRESS.md` — 本任务验收落盘

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`：工作区大量未提交（含本任务 5 处改动；未 commit/push）
- `git diff --stat`：15 files changed, 4204 insertions(+), 162 deletions(-)（含他人未提交改动，非本任务全部）
- Gerrit change：无（禁止提交）

## d. 验证/自测

- `python -m pytest tests/test_device_add_user.py -q` → **5 passed**，退出码 0
- `python -m pytest tests/test_credentials_role.py tests/test_device_capabilities_api.py tests/test_probe_source_guard.py tests/test_registration.py tests/test_device_acl.py -q` → **33 passed, 2 failed**，退出码 1
  - 2 个失败均在 `tests/test_registration.py`（`test_register_20_devices`、`test_probe_unreachable_registers_pending`），属既有失败：`app/seed.py` 的 `PRODUCT_TYPES=[]`（HEAD 已如此，v0.17 起移除 mock 机型种子），与本任务无关；本任务未触碰 MCP/seed。
- 既有 `tests/test_web_auth*.py`、`tests/test_catalog*.py` 不存在（`ls` 无匹配），故不适用。

## 备注：MCP 现状（只报告，未改）

- `app/mcp/server.py` `res_device_probe`（L489）、`res_device_register`（L517）**无 admin 判定**，仅 `require_auth()`；即 MCP 侧普通用户本就允许添加设备，与本次 Web 语义一致，无冲突。
- 机型维护 MCP 工具 `res_upsert_product_type`（约 L156）有 `if not auth.is_admin` → `FORBIDDEN`，保持仅管理员。
